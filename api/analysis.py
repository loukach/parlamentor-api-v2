"""Analysis stage configuration: system prompt, AnalysisOutput schema, model routing.

Analysis stage follows Research. The agent receives the DossierOutput from research
and produces AnalysisOutput with findings, story angles, and a recommendation.
Merges previous Analysis + Editorial into a single skill-mode call.
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
previous stage, identify newsworthy findings, and propose story angles.

### Your Task

You have been given a DossierOutput containing:
- Executive summary of research findings
- List of relevant legislative initiatives (with relevance notes)
- Observed voting patterns
- Voting summary (alignments and splits)
- Diplomas (published legislation)
- Media signals (recent headlines)
- Data gaps
- Recommended next steps

Your job is to:
1. **Analyze this data** and produce structured findings
2. **Propose 2-3 story angles** based on those findings

### Part 1: Findings

Each finding should be:
- **Newsworthy**: Does it reveal something surprising, significant, or contradictory?
- **Evidence-based**: Grounded in the dossier data, not speculation
- **Clear**: The headline and description should be understandable to a non-expert

**Finding Types:**
- **pattern**: Recurring behavior across multiple initiatives or votes
- **contradiction**: Party rhetoric vs voting behavior, or internal party splits
- **trend**: Change over time (requires historical comparison)
- **anomaly**: Unexpected outcome or surprising vote alignment
- **connection**: Link between different initiatives, parties, or events

**Newsworthiness Assessment:**
- **high**: Front-page material, major contradiction or pattern
- **medium**: Noteworthy but not shocking, good for feature stories
- **low**: Minor detail, useful context but not headline-worthy

**Evidence and Counter-Evidence:**
- **evidence**: List specific initiatives, votes, or data points
- **counter_evidence**: Note any data that contradicts or weakens this finding

### Part 2: Story Angles

Propose 2-3 distinct editorial angles — different ways to tell this story. Each angle should:
- **Have a clear thesis**: What's the main argument or narrative arc?
- **Outline the structure**: How would the story be organized?
- **Identify source gaps**: What additional interviews or research would strengthen this angle?

**Angle Types:**
- **accountability**: Holding specific actors accountable for contradictions or failures
- **systemic**: Examining systemic patterns or structural issues
- **comparative**: Comparing different parties, legislatures, or time periods
- **human_impact**: Focusing on how legislative action/inaction affects people
- **explainer**: Deep-dive explainer on a complex topic revealed by the data

### Recommendation

Recommend **one angle** and explain why. Consider newsworthiness, feasibility, and impact.

### Communication Style

- **Be direct**: No hedging language
- **Be specific**: Reference exact initiatives by ini_id, parties by name
- **Use markdown**: Bold **party names** and **ini_ids** in text fields. Use bullet lists where appropriate. Keep headlines and thesis fields as plain text.
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
- [ ] 2-3 story angles proposed
- [ ] Each angle has a clear thesis and structure
- [ ] One angle recommended with justification
- [ ] Executive summary captures the key insights\
"""

ANALYSIS_MODEL = settings.analysis_model or "claude-sonnet-4-6"
ANALYSIS_THINKING = {"type": "enabled", "budget_tokens": 16000}


async def build_analysis_prompt(
    dossier_output: dict,
    feedback: str | None = None,
) -> list[dict]:
    """Build the system prompt content blocks for the Analysis agent.

    Returns list of content blocks with cache_control markers.
    """
    identity = get_identity()
    instructions = fetch_prompt(_PROMPT_INSTRUCTIONS) or _FALLBACK_INSTRUCTIONS

    blocks = [
        {
            "type": "text",
            "text": identity,
            "cache_control": {"type": "ephemeral"},
        },
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
# AnalysisOutput JSON Schema — merged findings + story angles
# ---------------------------------------------------------------------------

ANALYSIS_SCHEMA = {
    "name": "analysis_output",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "executive_summary": {
                "type": "string",
                "description": "1 paragraph (3-5 sentences) summarizing the key editorial conclusions: what contradictions, patterns, or anomalies did the analysis uncover, and why do they matter journalistically. Do NOT re-summarize the research dossier data — focus on the analytical insights and their newsworthiness.",
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
                            "description": "Specific evidence from the dossier.",
                        },
                        "counter_evidence": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Data that contradicts or weakens this finding.",
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
            "story_angles": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {
                            "type": "string",
                            "description": "Unique identifier (UUID format) for this angle.",
                        },
                        "thesis": {
                            "type": "string",
                            "description": "1 sentence thesis statement.",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["accountability", "systemic", "comparative", "human_impact", "explainer"],
                            "description": "Type of editorial angle.",
                        },
                        "outline": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Story structure outline (section headings/descriptions).",
                        },
                        "key_findings": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Finding IDs central to this angle.",
                        },
                        "source_gaps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Additional sources or research needed.",
                        },
                    },
                    "required": ["id", "thesis", "type", "outline", "key_findings", "source_gaps"],
                    "additionalProperties": False,
                },
                "description": "Proposed story angles.",
            },
            "recommendation": {
                "type": "string",
                "description": "Which angle to pursue and why (2-3 sentences).",
            },
        },
        "required": ["executive_summary", "findings", "story_angles", "recommendation"],
        "additionalProperties": False,
    },
}
