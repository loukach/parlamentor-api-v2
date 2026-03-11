"""Editorial skill configuration: system prompt, EditorialOutput schema, model routing.

Editorial stage follows Analysis. The agent receives validated findings from the analysis gate
and produces editorial angles (story frameworks) for the journalist to choose from.
"""

import json
import logging

from api.prompts import fetch_prompt, get_identity

logger = logging.getLogger(__name__)

_PROMPT_INSTRUCTIONS = "parlamentor-v2-editorial-instructions"

# Hardcoded fallback instructions (>= 1024 tokens for caching)
_FALLBACK_INSTRUCTIONS = """\
## Editorial Phase Instructions

You are in the **Editorial** phase. Your goal is to propose editorial angles (story frameworks) \
based on the validated findings from the Analysis stage.

### Your Task

You have been given:
- A list of **validated findings** that the journalist approved
- A summary of the original research dossier

Your job is to propose 2-4 **editorial angles** — distinct ways to tell this story. Each angle should:
- **Have a clear thesis**: What's the main argument or narrative arc?
- **Be structurally sound**: How would the story be organized?
- **Identify source gaps**: What additional interviews, documents, or research would strengthen this angle?

### Angle Types

- **accountability**: Holding specific actors (parties, MPs, government) accountable for contradictions or failures
- **systemic**: Examining systemic patterns or structural issues in the legislative process
- **comparative**: Comparing different parties, legislatures, or time periods
- **human_impact**: Focusing on how legislative action/inaction affects real people
- **explainer**: Deep-dive explainer on a complex legislative topic revealed by the data

### Structure

For each angle, provide a **structure** — a rough outline of the story sections:
- **section**: Brief label (e.g., "Lede", "Context", "Main Evidence", "Counterpoint", "Conclusion")
- **content_summary**: 1-2 sentences describing what goes in this section

### Key Findings

For each angle, list which **finding IDs** (from the validated findings) are central to this angle. \
Not every finding needs to be used in every angle.

### Source Gaps

For each angle, identify what's missing:
- Interviews needed (which parties, MPs, experts)?
- Documents to request (committee reports, government responses)?
- External research (academic studies, comparable legislation in other countries)?

### Recommendation

At the end, recommend **one angle** and explain why. Consider:
- Newsworthiness (which angle has the strongest hook?)
- Feasibility (which angle can be reported most quickly with available resources?)
- Impact (which angle will resonate most with readers?)

### Communication Style

- **Be concrete**: Each angle should feel like a real story you could pitch to an editor
- **Be realistic**: Don't propose angles that require months of reporting or impossible access
- **Be creative**: Think beyond "Party X did Y" — what's the deeper story?

### Handling Revision Feedback

If the journalist sends you back for revision:
- Read the feedback carefully
- Adjust angles or propose new ones as requested
- Call request_gate_review again when done

### Quality Checklist

Before calling request_gate_review, verify:
- [ ] 2-4 distinct angles proposed
- [ ] Each angle has a clear thesis
- [ ] Structure outlines are concrete and logical
- [ ] Source gaps identified for each angle
- [ ] One angle recommended with justification

### Output Expectations

You will produce a structured EditorialOutput with:
- **angles**: List of editorial angles (thesis, type, structure, key_findings, source_gaps)
- **recommendation**: Which angle to pursue and why

Ensure your proposals are thorough enough to guide the journalist's next steps.\
"""

EDITORIAL_MODEL = "claude-sonnet-4-6"  # No thinking, no tools, fast single-call skill


async def build_editorial_prompt(
    validated_findings: list[dict],
    dossier_summary: str,
    feedback: str | None = None,
) -> list[dict]:
    """Build the system prompt content blocks for the Editorial skill.

    Returns list of content blocks with cache_control markers.
    Block 1 + Block 2 are cached (>= 1024 tokens each).
    Block 3 is dynamic (validated findings + dossier summary + feedback).

    Prompt text is fetched from Langfuse (production label) with
    hardcoded fallbacks if Langfuse is unavailable.
    """
    # Block 1: Shared identity
    identity = get_identity()
    # Block 2: Editorial-specific instructions
    instructions = fetch_prompt(_PROMPT_INSTRUCTIONS) or _FALLBACK_INSTRUCTIONS

    blocks = [
        # Block 1: Identity (cached)
        {
            "type": "text",
            "text": identity,
            "cache_control": {"type": "ephemeral"},
        },
        # Block 2: Editorial instructions (cached)
        {
            "type": "text",
            "text": instructions,
            "cache_control": {"type": "ephemeral"},
        },
    ]

    # Block 3: Dynamic context (not cached)
    dynamic_parts = [
        "## Validated Analysis Findings\n\n"
        "The journalist approved the following findings from the Analysis stage:\n\n"
        f"```json\n{json.dumps(validated_findings, indent=2, ensure_ascii=False)}\n```",
        f"\n\n## Research Dossier Summary\n\n{dossier_summary}",
    ]

    if feedback:
        dynamic_parts.append(
            f"\n\n## Revision Feedback\n\nThe journalist reviewed your previous editorial angles and "
            f"sent you back for revision with this feedback:\n\n{feedback}\n\n"
            f"Address each point in the feedback."
        )

    blocks.append({"type": "text", "text": "\n\n".join(dynamic_parts)})

    return blocks


# ---------------------------------------------------------------------------
# EditorialOutput JSON Schema (for structured extraction)
# ---------------------------------------------------------------------------

EDITORIAL_SCHEMA = {
    "name": "editorial_output",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "angles": {
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
                            "description": "1 sentence thesis statement for this editorial angle.",
                        },
                        "type": {
                            "type": "string",
                            "enum": ["accountability", "systemic", "comparative", "human_impact", "explainer"],
                            "description": "Type of editorial angle.",
                        },
                        "structure": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "section": {
                                        "type": "string",
                                        "description": "Section label (e.g., 'Lede', 'Context', 'Main Evidence').",
                                    },
                                    "content_summary": {
                                        "type": "string",
                                        "description": "1-2 sentences describing what goes in this section.",
                                    },
                                },
                                "required": ["section", "content_summary"],
                                "additionalProperties": False,
                            },
                            "description": "Story structure outline.",
                        },
                        "key_findings": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of finding IDs (from validated findings) central to this angle.",
                        },
                        "source_gaps": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of additional sources, interviews, or research needed for this angle.",
                        },
                    },
                    "required": ["id", "thesis", "type", "structure", "key_findings", "source_gaps"],
                    "additionalProperties": False,
                },
                "description": "List of proposed editorial angles.",
            },
            "recommendation": {
                "type": "string",
                "description": "Which angle to pursue and why (2-3 sentences).",
            },
        },
        "required": ["angles", "recommendation"],
        "additionalProperties": False,
    },
}
