"""Research stage configuration: analyst prompt, DossierOutput schema.

The research stage now works as a skill-mode analyst:
1. System pre-fetches data (initiatives, votes, diplomas, media) via prefetch.py
2. Data is shown to journalist in panel immediately
3. Sonnet analyzes pre-fetched data in a single skill-mode call
4. Produces DossierOutput (summary, patterns, gaps, curated references)

Escape hatch: raw_query tool available for ~1 in 5 investigations.
"""

import json
import logging

from api.prompts import fetch_prompt, get_identity

logger = logging.getLogger(__name__)

_PROMPT_INSTRUCTIONS = "parlamentor-v2-research-instructions"

# Fallback instructions for analyst mode (>= 1024 tokens for caching)
_FALLBACK_INSTRUCTIONS = """\
## Research Phase Instructions

You are in the **Research** phase. The system has already pre-fetched parliamentary data \
for this investigation topic. Your job is to **analyze the pre-fetched data** and produce \
a structured research dossier.

### What You Have

The system queried the parliamentary database and found:
- **Initiatives**: Bills, proposals, petitions matching the topic keywords
- **Votes**: Voting records for those initiatives
- **Diplomas**: Published legislation resulting from matched initiatives
- **Media signals**: Recent headlines from Portuguese media (if available)

This data is provided in the dynamic context below. Not all of it will be relevant — \
your job is to select and analyze only what pertains to the investigation topic.

### Your Task

Analyze the pre-fetched data and produce a DossierOutput containing:
1. **Executive summary**: 2-3 paragraphs on the key findings
2. **Topic keywords**: Relevant search terms (the system already expanded these, but refine if needed)
3. **Initiatives**: Select the most relevant ones, add a relevance_note for each
4. **Patterns**: Identify voting patterns, party alignments, legislative trends
5. **Voting summary**: Overview of voting dynamics (if applicable)
6. **Diplomas**: Relevant published legislation with context
7. **Media signals**: Recent media coverage providing context
8. **Data gaps**: What's missing that a journalist should investigate
9. **Recommended next steps**: Suggestions for deepening the investigation

### Analysis Methodology

1. **Filter for relevance**: Not all pre-fetched data is relevant. Focus on items directly \
related to the investigation topic. Discard noise.

2. **Cross-reference**: Link initiatives to their votes and diplomas. Which proposals became law? \
Which were rejected? Which are stuck in committee?

3. **Identify patterns**: Look for:
   - Parties that consistently vote together or against each other
   - Initiatives that stalled without a vote
   - Differences between binding legislation and non-binding resolutions
   - Government vs opposition dynamics

4. **Assess completeness**: Is the pre-fetched data sufficient? If you identify critical gaps, \
note them. The journalist can ask for more data via chat, and you have a raw_query escape hatch \
for specific SQL queries if truly needed.

5. **Be honest about limitations**: If the data doesn't support a conclusion, say so.

### Communication Style

- **Be direct**: No hedging or filler
- **Use Portuguese parliamentary terms** when discussing specific concepts
- **Be specific**: Reference initiatives by ini_id, parties by name
- **Think critically**: Don't just restate the data — analyze what it means

### Handling User Messages

The journalist may ask for more data or adjustments:
- If they ask to check a specific party or topic, use the raw_query tool
- If they provide additional context, incorporate it into your analysis
- After addressing their request, update your DossierOutput

### Using raw_query (Escape Hatch)

You have access to a `raw_query` tool for direct SQL queries against the parliamentary database. \
Use it ONLY when the pre-fetched data clearly lacks a critical dimension. Most investigations \
should not need it. Always call `describe_table` first to check column names.

### Quality Checklist

Before producing your output, verify:
- [ ] You've filtered pre-fetched data for relevance
- [ ] You've identified at least one notable pattern or finding
- [ ] You've cross-referenced initiatives with votes and diplomas
- [ ] You've noted data gaps honestly
- [ ] Relevance notes explain why each initiative matters
- [ ] Executive summary captures the key insights\
"""


async def build_research_prompt(
    topic: str,
    prefetch_data: dict | None = None,
    feedback: str | None = None,
) -> list[dict]:
    """Build the system prompt content blocks for the Research analyst.

    Args:
        topic: Investigation topic
        prefetch_data: Pre-fetched data dict (initiatives, votes, diplomas)
        feedback: Revision feedback from journalist

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

    # Block 3: Dynamic context (topic + pre-fetched data + feedback)
    dynamic_parts = [f"## Current Investigation\n\nTopic: {topic}"]

    if prefetch_data:
        stats = prefetch_data.get("stats", {})
        dynamic_parts.append(
            f"\n\n## Pre-Fetched Data\n\n"
            f"The system found {stats.get('initiative_count', 0)} initiatives, "
            f"{stats.get('vote_count', 0)} votes, and "
            f"{stats.get('diploma_count', 0)} diplomas.\n\n"
            f"### Initiatives\n```json\n"
            f"{json.dumps(prefetch_data.get('initiatives', []), indent=2, ensure_ascii=False)}\n```\n\n"
            f"### Votes\n```json\n"
            f"{json.dumps(prefetch_data.get('votes', []), indent=2, ensure_ascii=False)}\n```\n\n"
            f"### Diplomas\n```json\n"
            f"{json.dumps(prefetch_data.get('diplomas', []), indent=2, ensure_ascii=False)}\n```"
        )
        if prefetch_data.get("media_signals"):
            dynamic_parts.append(
                f"\n\n### Media Signals\n```json\n"
                f"{json.dumps(prefetch_data['media_signals'], indent=2, ensure_ascii=False)}\n```"
            )

    if feedback:
        dynamic_parts.append(
            f"\n\n## Revision Feedback\n\nThe journalist reviewed your previous research and "
            f"sent you back for revision with this feedback:\n\n{feedback}\n\n"
            f"Address each point in the feedback. Focus your additional research on the gaps identified."
        )

    blocks.append({"type": "text", "text": "\n\n".join(dynamic_parts)})

    return blocks


# ---------------------------------------------------------------------------
# DossierOutput JSON Schema (for structured extraction)
# ---------------------------------------------------------------------------

DOSSIER_SCHEMA = {
    "name": "dossier_output",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "executive_summary": {
                "type": "string",
                "description": "2-3 paragraphs summarizing the research findings.",
            },
            "topic_keywords": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Key search terms and relevant keywords.",
            },
            "initiatives": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "ini_id": {"type": "string"},
                        "title": {"type": "string"},
                        "party": {"type": "string"},
                        "type_description": {"type": "string"},
                        "status": {"type": "string"},
                        "vote_result": {"type": ["string", "null"]},
                        "summary": {"type": ["string", "null"]},
                        "relevance_note": {"type": "string"},
                    },
                    "required": [
                        "ini_id", "title", "party", "type_description",
                        "status", "vote_result", "summary", "relevance_note",
                    ],
                    "additionalProperties": False,
                },
                "description": "Relevant initiatives curated from pre-fetched data.",
            },
            "patterns": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string"},
                        "evidence": {"type": "string"},
                        "parties_involved": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["description", "evidence", "parties_involved"],
                    "additionalProperties": False,
                },
                "description": "Observed voting patterns or legislative trends.",
            },
            "voting_summary": {
                "type": ["object", "null"],
                "properties": {
                    "total_votes_found": {"type": "integer"},
                    "notable_alignments": {"type": "string"},
                    "notable_splits": {"type": "string"},
                },
                "required": ["total_votes_found", "notable_alignments", "notable_splits"],
                "additionalProperties": False,
                "description": "Overview of voting dynamics, or null if not applicable.",
            },
            "diplomas": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "diploma_id": {"type": ["integer", "null"]},
                        "tipo": {"type": "string"},
                        "numero": {"type": ["string", "null"]},
                        "titulo": {"type": ["string", "null"]},
                        "pub_date": {"type": ["string", "null"]},
                        "related_initiatives": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "relevance_note": {"type": "string"},
                    },
                    "required": [
                        "diploma_id", "tipo", "numero", "titulo",
                        "pub_date", "related_initiatives", "relevance_note",
                    ],
                    "additionalProperties": False,
                },
                "description": "Relevant diplomas (published legislation).",
            },
            "media_signals": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "headline": {"type": "string"},
                        "source": {"type": "string"},
                        "url": {"type": "string"},
                        "date": {"type": ["string", "null"]},
                        "relevance_note": {"type": "string"},
                    },
                    "required": ["headline", "source", "url", "date", "relevance_note"],
                    "additionalProperties": False,
                },
                "description": "Media headlines providing context.",
            },
            "data_gaps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "What's missing or needs further investigation.",
            },
            "recommended_next_steps": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Suggestions for the Analysis phase.",
            },
        },
        "required": [
            "executive_summary", "topic_keywords", "initiatives", "patterns",
            "voting_summary", "diplomas", "media_signals",
            "data_gaps", "recommended_next_steps",
        ],
        "additionalProperties": False,
    },
}
