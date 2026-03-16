"""Drafting stage configuration: prompt, DraftOutput schema, Opus 4.6.

Drafting is chat-based iteration:
- Each message (feedback) → single Opus call → complete DraftOutput
- Agent receives: angle + DossierOutput + previous draft + feedback → new draft
- No gate. save_output(set_gate_pending=False) saves each version.
"""

import json
import logging

from api.config import settings
from api.prompts import fetch_prompt, get_identity

logger = logging.getLogger(__name__)

_PROMPT_INSTRUCTIONS = "parlamentor-v2-drafting-instructions"

DRAFTING_MODEL = settings.drafting_model or "claude-opus-4-6"
DRAFTING_THINKING = {"type": "enabled", "budget_tokens": 10000}

# Fallback instructions (>= 1024 tokens for caching)
_FALLBACK_INSTRUCTIONS = """\
## Drafting Phase Instructions

You are in the **Drafting** phase. Your goal is to write a complete, publication-ready \
investigative article based on the journalist's chosen story angle and the research data.

### What You Have

- **Story angle**: The journalist selected (or wrote) an angle with a thesis and structure
- **Research dossier**: Findings, initiatives, votes, diplomas, and media signals
- **Analysis**: Findings with evidence and newsworthiness ratings
- **Previous draft** (if this is a revision): The last version of the article
- **Journalist feedback** (if this is a revision): Specific feedback on what to change

### Article Standards

Write as a professional Portuguese investigative journalist. The article should:

1. **Be factual**: Every claim must be traceable to data in the dossier or analysis
2. **Be well-structured**: Clear sections with logical flow
3. **Be engaging**: Strong lede, compelling narrative arc, concrete examples
4. **Be fair**: Present counter-evidence where it exists, give context to numbers
5. **Be Portuguese**: Write in PT-PT with correct parliamentary terminology

### Citations

Every factual claim needs a citation. Use this format:
- Reference the source (e.g., "Projeto de Lei 123/XVI", "votacao de 15 de Maio de 2025")
- Include source_id (ini_id or vote id) for traceability
- Group citations at the end of the article

### Sections

Structure your article with clear sections. Typical structure:
- **Titulo**: Compelling, specific headline (not clickbait)
- **Subtitulo**: 1-sentence expansion of the headline
- **Lede**: Opening paragraph that hooks the reader with the key finding
- **Contexto**: Background on the topic and why it matters
- **Dados**: Deep dive into the evidence (initiatives, votes, patterns)
- **Contraditorio**: Counter-evidence, opposing perspectives, or alternative explanations
- **Conclusao**: What this means going forward, open questions

### Handling Revisions

When the journalist sends feedback:
- Address each point specifically
- Preserve what they liked (don't rewrite from scratch)
- Show what changed (the journalist will compare versions)
- If feedback is vague, make your best judgment call

### Quality Checklist

- [ ] Every factual claim has a citation
- [ ] Counter-evidence is acknowledged
- [ ] No speculation beyond what the data shows
- [ ] Article flows logically from section to section
- [ ] Language is clear and accessible
- [ ] Word count is appropriate (500-2000 words typical)\
"""


async def build_drafting_prompt(
    angle: dict,
    dossier_output: dict,
    analysis_output: dict,
    previous_draft: dict | None = None,
    feedback: str | None = None,
) -> list[dict]:
    """Build the system prompt for the Drafting agent.

    Args:
        angle: The selected story angle (from AnalysisOutput.story_angles)
        dossier_output: Research dossier data
        analysis_output: Analysis findings + angles
        previous_draft: Previous DraftOutput (for revisions)
        feedback: Journalist feedback text
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

    # Block 3: Dynamic context
    dynamic_parts = [
        "## Selected Story Angle\n\n"
        f"```json\n{json.dumps(angle, indent=2, ensure_ascii=False)}\n```",
        "\n\n## Research Dossier\n\n"
        f"```json\n{json.dumps(dossier_output, indent=2, ensure_ascii=False)}\n```",
        "\n\n## Analysis Findings\n\n"
        f"```json\n{json.dumps(analysis_output, indent=2, ensure_ascii=False)}\n```",
    ]

    if previous_draft:
        dynamic_parts.append(
            "\n\n## Previous Draft (revise this)\n\n"
            f"```json\n{json.dumps(previous_draft, indent=2, ensure_ascii=False)}\n```"
        )

    if feedback:
        dynamic_parts.append(
            f"\n\n## Journalist Feedback\n\n{feedback}\n\n"
            f"Address each point in the feedback."
        )

    blocks.append({"type": "text", "text": "\n\n".join(dynamic_parts)})

    return blocks


# ---------------------------------------------------------------------------
# DraftOutput JSON Schema
# ---------------------------------------------------------------------------

DRAFT_SCHEMA = {
    "name": "draft_output",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Article headline.",
            },
            "subtitle": {
                "type": "string",
                "description": "1-sentence subtitle/subheadline.",
            },
            "sections": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "heading": {
                            "type": "string",
                            "description": "Section heading.",
                        },
                        "content": {
                            "type": "string",
                            "description": "Section content in markdown.",
                        },
                    },
                    "required": ["heading", "content"],
                    "additionalProperties": False,
                },
                "description": "Article sections in order.",
            },
            "citations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Citation text (what was referenced).",
                        },
                        "source": {
                            "type": "string",
                            "description": "Source description (e.g., 'Projeto de Lei 123/XVI').",
                        },
                        "source_id": {
                            "type": "string",
                            "description": "Machine-readable source ID (ini_id, vote id, etc.).",
                        },
                    },
                    "required": ["text", "source", "source_id"],
                    "additionalProperties": False,
                },
                "description": "List of citations/references.",
            },
            "word_count": {
                "type": "integer",
                "description": "Approximate word count of the article.",
            },
        },
        "required": ["title", "subtitle", "sections", "citations", "word_count"],
        "additionalProperties": False,
    },
}
