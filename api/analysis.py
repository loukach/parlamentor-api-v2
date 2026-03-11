"""Analysis agent configuration: system prompt, AnalysisOutput schema, model routing.

Analysis stage follows Research. The agent receives the DossierOutput from research
and produces structured AnalysisOutput with findings, executive summary, and meta notes.
"""

import json
import logging

from api.config import settings
from api.prompts import fetch_prompt, get_identity

logger = logging.getLogger(__name__)

_PROMPT_INSTRUCTIONS = "parlamentor-v2-analysis-instructions"

# Hardcoded fallback instructions (>= 1024 tokens for caching)
_FALLBACK_INSTRUCTIONS = """\
## Analysis Phase Instructions

You are in the **Analysis** phase. Your goal is to analyze the research dossier produced in the \
previous stage and identify newsworthy findings.

### Your Task

You have been given a DossierOutput containing:
- Executive summary of research findings
- List of relevant legislative initiatives
- Observed voting patterns
- Voting summary (alignments and splits)
- Data gaps
- Recommended next steps

Your job is to **analyze this data** and produce structured findings. Each finding should be:
- **Newsworthy**: Does it reveal something surprising, significant, or contradictory?
- **Evidence-based**: Grounded in the dossier data, not speculation
- **Clear**: The headline and description should be understandable to a non-expert

### Finding Types

- **pattern**: Recurring behavior across multiple initiatives or votes
- **contradiction**: Party rhetoric vs voting behavior, or internal party splits
- **trend**: Change over time (requires historical comparison)
- **anomaly**: Unexpected outcome or surprising vote alignment
- **connection**: Link between different initiatives, parties, or events

### Newsworthiness Assessment

For each finding, rate its newsworthiness:
- **high**: Front-page material, major contradiction or pattern
- **medium**: Noteworthy but not shocking, good for feature stories
- **low**: Minor detail, useful context but not headline-worthy

### Evidence and Counter-Evidence

- **evidence**: List specific initiatives, votes, or data points from the dossier that support this finding
- **counter_evidence**: Note any data that contradicts or weakens this finding (if any)

### Executive Summary

Write a 1-paragraph overview (3-5 sentences) summarizing the most significant findings. \
This will help the journalist quickly understand the analysis results.

### Meta Notes

In the `meta` section:
- **dossier_coverage**: Comment on the quality and completeness of the research dossier
- **confidence_notes**: Note any caveats, data quality issues, or areas where additional research would strengthen findings

### Communication Style

- **Be direct**: No hedging language ("it seems that", "perhaps", "it might be")
- **Be specific**: Reference exact initiatives by ini_id, parties by name
- **Be honest**: If evidence is thin or ambiguous, say so explicitly
- **Think critically**: Don't just restate patterns from the dossier — analyze what they mean

### Handling Revision Feedback

If the journalist sends you back for revision:
- Read the feedback carefully
- Address each point specifically
- Adjust findings or add new ones as requested
- Call request_gate_review again when done

### Quality Checklist

Before calling request_gate_review, verify:
- [ ] At least 3 findings identified
- [ ] Each finding has clear evidence from the dossier
- [ ] Newsworthiness ratings are justified
- [ ] Executive summary captures the key insights
- [ ] Counter-evidence noted where relevant
- [ ] No speculation beyond what the data shows

### Output Expectations

After calling request_gate_review, you will produce a structured AnalysisOutput with:
- **executive_summary**: 1 paragraph overview of findings
- **findings**: List of structured findings (headline, description, evidence, type, newsworthiness)
- **meta**: Dossier coverage notes and confidence assessment

Ensure your analysis is thorough enough to populate all these fields meaningfully.\
"""

ANALYSIS_MODEL = settings.analysis_model or "claude-sonnet-4-6"
ANALYSIS_THINKING = {"type": "enabled", "budget_tokens": 16000}


async def build_analysis_prompt(
    dossier_output: dict,
    feedback: str | None = None,
) -> list[dict]:
    """Build the system prompt content blocks for the Analysis agent.

    Returns list of content blocks with cache_control markers.
    Block 1 + Block 2 are cached (>= 1024 tokens each).
    Block 3 is dynamic (dossier + feedback).

    Prompt text is fetched from Langfuse (production label) with
    hardcoded fallbacks if Langfuse is unavailable.
    """
    # Block 1: Shared identity
    identity = get_identity()
    # Block 2: Analysis-specific instructions
    instructions = fetch_prompt(_PROMPT_INSTRUCTIONS) or _FALLBACK_INSTRUCTIONS

    blocks = [
        # Block 1: Identity (cached)
        {
            "type": "text",
            "text": identity,
            "cache_control": {"type": "ephemeral"},
        },
        # Block 2: Analysis instructions (cached)
        {
            "type": "text",
            "text": instructions,
            "cache_control": {"type": "ephemeral"},
        },
    ]

    # Block 3: Dynamic context (not cached)
    dynamic_parts = [
        "## Research Dossier to Analyze\n\n"
        "The research stage produced the following dossier:\n\n"
        f"```json\n{json.dumps(dossier_output, indent=2, ensure_ascii=False)}\n```"
    ]

    if feedback:
        dynamic_parts.append(
            f"\n\n## Revision Feedback\n\nThe journalist reviewed your previous analysis and "
            f"sent you back for revision with this feedback:\n\n{feedback}\n\n"
            f"Address each point in the feedback. Focus on the gaps or adjustments identified."
        )

    blocks.append({"type": "text", "text": "\n\n".join(dynamic_parts)})

    return blocks


# ---------------------------------------------------------------------------
# AnalysisOutput JSON Schema (for structured extraction)
# ---------------------------------------------------------------------------

ANALYSIS_SCHEMA = {
    "name": "analysis_output",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "executive_summary": {
                "type": "string",
                "description": "1 paragraph (3-5 sentences) overview of the most significant findings.",
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Unique identifier (UUID format) for this finding.",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["pattern", "contradiction", "trend", "anomaly", "connection"],
                            "description": "Type of finding.",
                        },
                        "headline": {
                            "type": "string",
                            "description": "1 sentence headline summarizing the finding.",
                        },
                        "description": {
                            "type": "string",
                            "description": "2-3 paragraphs explaining the finding in detail.",
                        },
                        "evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of specific evidence from the dossier (initiative IDs, vote records, patterns).",
                        },
                        "counter_evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of data that contradicts or weakens this finding (if any).",
                        },
                        "newsworthiness": {
                            "type": "string",
                            "enum": ["high", "medium", "low"],
                            "description": "Newsworthiness rating.",
                        },
                        "newsworthiness_rationale": {
                            "type": "string",
                            "description": "Brief justification for the newsworthiness rating.",
                        },
                    },
                    "required": [
                        "id", "type", "headline", "description", "evidence",
                        "counter_evidence", "newsworthiness", "newsworthiness_rationale",
                    ],
                    "additionalProperties": False,
                },
                "description": "List of analysis findings.",
            },
            "meta": {
                "type": "object",
                "properties": {
                    "dossier_coverage": {
                        "type": "string",
                        "description": "Assessment of the research dossier quality and completeness.",
                    },
                    "confidence_notes": {
                        "type": "string",
                        "description": "Caveats, data quality issues, or areas needing additional research.",
                    },
                },
                "required": ["dossier_coverage", "confidence_notes"],
                "additionalProperties": False,
                "description": "Meta notes about the analysis.",
            },
        },
        "required": ["executive_summary", "findings", "meta"],
        "additionalProperties": False,
    },
}
