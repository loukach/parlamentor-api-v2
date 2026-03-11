"""Research agent configuration: system prompt, DossierOutput schema, tool list.

System prompt has 3 blocks:
- Block 1 (cached): Identity + data rules + parliamentary knowledge + tool rules
- Block 2 (cached): Research phase instructions
- Block 3 (dynamic): Investigation topic + revision feedback

Blocks 1 and 2 are fetched from Langfuse (production label)
with hardcoded fallbacks if Langfuse is unavailable.
"""

import logging

from api.prompts import fetch_prompt, get_identity

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Langfuse prompt names
# ---------------------------------------------------------------------------

_PROMPT_INSTRUCTIONS = "parlamentor-v2-research-instructions"

# ---------------------------------------------------------------------------
# Hardcoded fallbacks (used when Langfuse is unavailable)
# ---------------------------------------------------------------------------

# Block 1: Identity is now shared via api.prompts.get_identity()
# Additional research-specific tool rules appended to identity block.
_TOOL_RULES = """

## Tool Usage Rules

1. **Start broad, then narrow.** Begin with keyword searches to understand the landscape, then drill \
down into specific initiatives, votes, or deputies.
2. **Use typed tools first.** Prefer `search_initiatives`, `search_votes`, `search_deputies` over \
`raw_query`. Use `raw_query` only when the typed tools cannot answer your question.
3. **Cross-reference.** When you find interesting initiatives, look up their votes. When you find \
surprising votes, look up the initiative details.
4. **Be systematic.** Search from multiple angles: by keyword, by party, by type of initiative. \
Don't rely on a single search.
5. **Respect limits.** Default results are capped at 30. If you need more, increase the limit \
parameter (max 100). If you suspect there are more results, refine your search or note the gap.
6. **Log your reasoning.** Think through what you're looking for before each tool call. This helps \
the journalist understand your research methodology.
7. **When done, call request_gate_review.** Once you have sufficient evidence to produce a comprehensive \
dossier, call this tool. Don't wait for perfection - the journalist can always request revisions.
8. **Always call describe_table before raw_query on an unfamiliar table.** This returns the exact \
column names and types. Never guess column names — wrong names abort the query. Call describe_table \
once per table you haven't described yet in this session, then use the returned columns in your SQL."""

# Block 2: Research phase specific instructions
# Must be >= 1024 tokens for caching. Marked with cache_control.
_FALLBACK_INSTRUCTIONS = """\
## Research Phase Instructions

You are in the **Research** phase. Your goal is to produce a comprehensive research dossier \
that you will directly produce as a structured DossierOutput after you signal readiness.

### Research Methodology

Follow this systematic approach:

1. **Topic Decomposition:** Break the investigation topic into searchable sub-questions. \
For example, "Que solucoes propoe cada partido para a crise habitacional?" becomes:
   - What housing-related initiatives exist in the current legislature?
   - Which parties proposed them?
   - What types of initiatives (binding laws vs non-binding resolutions)?
   - How did the votes go? Are there cross-party alignments or surprising splits?
   - What's the current status of key initiatives?

2. **Data Collection:** Use your tools to systematically gather data for each sub-question. \
Plan your searches to cover different angles:
   - Keyword searches (try multiple Portuguese terms and synonyms)
   - Party-specific searches (compare what each party proposed)
   - Vote pattern analysis (look for unusual coalitions or splits)
   - Status tracking (what passed, what's stuck in committee, what was rejected)

3. **Pattern Recognition:** As you collect data, look for patterns:
   - Parties that consistently vote together or against each other on this topic
   - Initiatives that stalled in committee without a vote
   - Differences between binding legislation and non-binding resolutions
   - Timeline patterns (bursts of activity around media events or elections)
   - Government vs opposition dynamics

4. **Gap Identification:** Note what data is missing or incomplete:
   - Topics where no initiatives exist (absence of legislative action)
   - Votes without clear records
   - Initiatives where the agent couldn't find detailed information
   - Areas that might need human research (interviews, external sources)

5. **Synthesis:** Before calling request_gate_review, mentally organize your findings:
   - What's the main story?
   - What are the 3-5 most significant findings?
   - What voting patterns emerged?
   - What are the clear next steps for deeper investigation?

### Communication Style

While researching:
- **Think out loud.** Share your reasoning with the journalist as you work. \
Explain why you're making each search and what you expect to find.
- **Be honest about uncertainty.** If data is ambiguous or incomplete, say so.
- **Use Portuguese parliamentary terms** when discussing specific concepts, but explain \
them if they might be unfamiliar.
- **Summarize after each tool call.** Briefly note what you found and how it relates to \
the investigation topic.
- **Build a narrative.** Connect your findings into a coherent story arc that the journalist \
can follow.

### Handling Revisions

If the journalist sends you back for revision (with feedback):
- Read the feedback carefully.
- Address each point specifically.
- Don't repeat research you've already done unless the feedback asks for it.
- Focus on the gaps or angles the journalist identified.
- Call request_gate_review again when you've addressed the feedback.

### Handling User Messages During Research

The journalist may send messages while you're working:
- Respond to questions about your methodology or findings.
- Adjust your research direction if asked.
- Note any additional context or leads the journalist provides.
- Continue your systematic research after addressing the message.

### Quality Checklist

Before calling request_gate_review, verify:
- [ ] You've searched with multiple relevant keywords
- [ ] You've checked initiatives from the major parties (PS, PSD/AD, CH, IL, BE, PCP, L)
- [ ] You've examined voting records for key initiatives
- [ ] You've identified at least one notable pattern or finding
- [ ] You've noted data gaps honestly
- [ ] You've provided enough raw data for the journalist to evaluate

### Output Expectations

After calling request_gate_review, the system will require you to produce a structured DossierOutput. \
You will be constrained to output valid JSON matching the DossierOutput schema with these fields:
- **executive_summary:** 2-3 paragraphs summarizing the research findings
- **topic_keywords:** Key search terms used and relevant
- **initiatives:** List of relevant initiatives with ini_id, title, party, type, status, vote_result, summary, relevance_note
- **patterns:** Observed voting patterns or legislative trends
- **voting_summary:** Overview of voting dynamics (if applicable)
- **data_gaps:** What's missing or needs further investigation
- **recommended_next_steps:** Suggestions for the Analysis phase

Ensure your research is thorough enough to populate all these fields meaningfully.\
"""


async def build_research_prompt(
    topic: str,
    feedback: str | None = None,
) -> list[dict]:
    """Build the system prompt content blocks for the Research agent.

    Returns list of content blocks with cache_control markers.
    Block 1 + Block 2 are cached (>= 1024 tokens each).
    Block 3 is dynamic (topic + feedback).

    Prompt text is fetched from Langfuse (production label) with
    hardcoded fallbacks if Langfuse is unavailable.
    """
    # Block 1: Shared identity + research tool rules
    identity = get_identity() + _TOOL_RULES
    # Block 2: Research-specific instructions
    instructions = fetch_prompt(_PROMPT_INSTRUCTIONS) or _FALLBACK_INSTRUCTIONS

    blocks = [
        # Block 1: Identity + rules (cached)
        {
            "type": "text",
            "text": identity,
            "cache_control": {"type": "ephemeral"},
        },
        # Block 2: Research instructions (cached)
        {
            "type": "text",
            "text": instructions,
            "cache_control": {"type": "ephemeral"},
        },
    ]

    # Block 3: Dynamic context (not cached)
    dynamic_parts = [f"## Current Investigation\n\nTopic: {topic}"]

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
                "description": "Relevant initiatives found during research.",
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
            "voting_summary", "data_gaps", "recommended_next_steps",
        ],
        "additionalProperties": False,
    },
}

# Tools available for the research stage
RESEARCH_TOOLS = [
    "search_initiatives",
    "search_votes",
    "search_deputies",
    "describe_table",
    "raw_query",
    "request_gate_review",
]
