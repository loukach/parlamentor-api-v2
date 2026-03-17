"""Pre-fetch pipeline: keyword expansion + batch SQL queries.

Replaces the 15-25 tool-call agent loop with:
1. Haiku expands topic into Portuguese search keywords (~$0.001)
2. System runs 3 parallel SQL queries against Parla DB (~0 cost, <1s)
3. Data shown in panel instantly, before agent starts analyzing

Returns a ResearchPackage dict with initiatives, votes, diplomas.
"""

import asyncio
import json
import logging
import time

import anthropic

from api.config import settings
from api.costs import calculate_cost

logger = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None


def _get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
    return _client


KEYWORD_MODEL = "claude-haiku-4-5-20251001"


async def expand_keywords(topic: str) -> tuple[list[str], dict]:
    """Use Haiku to expand a topic into 10-15 Portuguese search keywords.

    Returns (keywords, usage_dict) where usage_dict has token counts + cost.
    """
    t0 = time.monotonic()
    response = await _get_client().messages.create(
        model=KEYWORD_MODEL,
        max_tokens=512,
        messages=[{"role": "user", "content": topic}],
        system=[{
            "type": "text",
            "text": (
                "You are a Portuguese parliamentary research assistant. "
                "Given a topic, produce 10-15 search keywords in Portuguese that cover:\n"
                "- Direct terms and synonyms\n"
                "- Legislative/technical terms (e.g. 'projeto de lei', 'proposta de resolucao')\n"
                "- Related policy areas\n"
                "- Common abbreviations (e.g. 'IMI', 'IHRU')\n\n"
                "Output ONLY a JSON array of strings. No explanation."
            ),
        }],
    )
    duration_ms = int((time.monotonic() - t0) * 1000)

    # Parse keywords from response (strip markdown fences if present)
    text = response.content[0].text.strip()
    if text.startswith("```"):
        # Remove ```json ... ``` fences
        lines = text.split("\n")
        text = "\n".join(
            line for line in lines if not line.strip().startswith("```")
        ).strip()
    try:
        keywords = json.loads(text)
        if not isinstance(keywords, list):
            keywords = [topic]
    except json.JSONDecodeError:
        logger.warning("Failed to parse keyword expansion, using topic as-is: %s", text)
        keywords = [topic]

    usage = response.usage
    input_tokens = usage.input_tokens
    output_tokens = usage.output_tokens
    cost = calculate_cost(KEYWORD_MODEL, input_tokens, output_tokens, 0, 0)

    usage_dict = {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
        "cost_usd": cost,
        "duration_ms": duration_ms,
        "model": KEYWORD_MODEL,
    }

    logger.info("Keyword expansion: %d keywords in %dms", len(keywords), duration_ms)
    return keywords, usage_dict


async def batch_fetch(
    parla_session_factory,
    topic: str,
    keywords: list[str],
) -> dict:
    """Run 3 parallel SQL queries against Parla DB.

    Returns dict with keys: initiatives, votes, diplomas, stats.
    """
    from sqlalchemy import text

    # Build ILIKE patterns from keywords (stem-friendly partial matching)
    ilike_patterns = []
    for kw in keywords:
        # Extract root for partial matching (e.g. 'habitacao' -> 'habitac')
        root = kw.lower()
        if len(root) > 5:
            root = root[:len(root) - 2]  # rough stemming
        ilike_patterns.append(f"%{root}%")

    # Build the tsquery from topic + keywords
    ts_terms = " ".join([topic] + keywords[:5])

    async def _fetch_initiatives(session):
        """Fetch initiatives matching keywords, ranked by relevance."""
        # Build OR conditions for ILIKE
        ilike_conditions = " OR ".join(
            f"title ILIKE :pat_{i}" for i in range(len(ilike_patterns))
        )
        bind = {f"pat_{i}": p for i, p in enumerate(ilike_patterns)}
        bind["ts_query"] = ts_terms
        bind["legislature"] = "XVII"

        sql = f"""
            SELECT ini_id, title, type_description, author_name, current_status,
                   COALESCE(llm_summary, summary) AS summary,
                   ts_rank(
                       to_tsvector('portuguese', title || ' ' || COALESCE(summary, '')),
                       plainto_tsquery('portuguese', :ts_query)
                   ) AS rank
            FROM iniciativas
            WHERE legislature = :legislature
              AND ({ilike_conditions}
                   OR to_tsvector('portuguese', title || ' ' || COALESCE(summary, ''))
                      @@ plainto_tsquery('portuguese', :ts_query))
            ORDER BY rank DESC
            LIMIT 100
        """
        t0 = time.monotonic()
        result = await session.execute(text(sql), bind)
        rows = result.mappings().all()
        duration = int((time.monotonic() - t0) * 1000)
        logger.info("Prefetch initiatives: %d rows in %dms", len(rows), duration)
        return [dict(r) for r in rows]

    async def _fetch_votes(session, ini_ids: list[str]):
        """Fetch votes for matched initiatives."""
        if not ini_ids:
            return []
        placeholders = ", ".join(f":id_{i}" for i in range(len(ini_ids)))
        bind = {f"id_{i}": v for i, v in enumerate(ini_ids)}

        sql = f"""
            SELECT v.id, v.iniciativa_id, i.ini_id, i.title, i.author_name,
                   v.phase_name, v.vote_date, v.resultado, v.unanime,
                   v.favor, v.contra, v.abstencao
            FROM votes v
            JOIN iniciativas i ON i.id = v.iniciativa_id
            WHERE i.ini_id IN ({placeholders})
            ORDER BY v.vote_date DESC NULLS LAST
        """
        t0 = time.monotonic()
        result = await session.execute(text(sql), bind)
        rows = result.mappings().all()
        duration = int((time.monotonic() - t0) * 1000)
        logger.info("Prefetch votes: %d rows in %dms", len(rows), duration)
        return [dict(r) for r in rows]

    async def _fetch_diplomas(session, ini_ids: list[str]):
        """Fetch diplomas for matched initiatives."""
        if not ini_ids:
            return []
        placeholders = ", ".join(f":id_{i}" for i in range(len(ini_ids)))
        bind = {f"id_{i}": v for i, v in enumerate(ini_ids)}

        sql = f"""
            SELECT d.id, d.tipo, d.numero, d.titulo, d.pub_date,
                   di.ini_id
            FROM diplomas d
            JOIN diploma_iniciativas di ON di.diploma_id = d.id
            WHERE di.ini_id IN ({placeholders})
            ORDER BY d.pub_date DESC NULLS LAST
        """
        t0 = time.monotonic()
        result = await session.execute(text(sql), bind)
        rows = result.mappings().all()
        duration = int((time.monotonic() - t0) * 1000)
        logger.info("Prefetch diplomas: %d rows in %dms", len(rows), duration)
        return [dict(r) for r in rows]

    # Step 1: fetch initiatives
    async with parla_session_factory() as session:
        initiatives = await _fetch_initiatives(session)

    ini_ids = [r["ini_id"] for r in initiatives if r.get("ini_id")]

    # Step 2: fetch votes + diplomas in parallel
    async with parla_session_factory() as session_v:
        async with parla_session_factory() as session_d:
            votes, diplomas = await asyncio.gather(
                _fetch_votes(session_v, ini_ids),
                _fetch_diplomas(session_d, ini_ids),
            )

    # Serialize date fields
    for row in initiatives:
        for k, v in row.items():
            if hasattr(v, "isoformat"):
                row[k] = v.isoformat()

    for row in votes:
        for k, v in row.items():
            if hasattr(v, "isoformat"):
                row[k] = v.isoformat()

    for row in diplomas:
        for k, v in row.items():
            if hasattr(v, "isoformat"):
                row[k] = v.isoformat()

    return {
        "initiatives": initiatives,
        "votes": votes,
        "diplomas": diplomas,
        "stats": {
            "initiative_count": len(initiatives),
            "vote_count": len(votes),
            "diploma_count": len(diplomas),
        },
    }
