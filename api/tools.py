"""DB search tools for the research agent.

6 tools: search_initiatives, search_votes, search_deputies, describe_table,
raw_query, request_gate_review.
All typed tools generate SQL internally (agent never writes SQL for these).
"""

import logging
import re
import time
import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.models import QueryLog

logger = logging.getLogger(__name__)

DEFAULT_LEGISLATURE = "XVII"
DEFAULT_LIMIT = 30
MAX_LIMIT = 100

PARLA_TABLES = frozenset({
    "iniciativas", "iniciativa_events", "votes", "deputados",
    "intervencoes", "speech_transcripts", "iniciativa_autores", "iniciativa_comissao",
    "diplomas", "diploma_iniciativas",
})


# ---------------------------------------------------------------------------
# Tool definitions (Anthropic tool format)
# ---------------------------------------------------------------------------

TOOL_DEFINITIONS: dict[str, dict] = {
    "search_initiatives": {
        "name": "search_initiatives",
        "description": (
            "Search legislative initiatives (bills, proposals, petitions) in the Portuguese parliament. "
            "Returns initiative details including title, party, type, status, and vote results. "
            "Use keywords to search in titles and summaries. Filter by party, type, or legislature."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Search keywords to match against initiative titles and summaries. Use Portuguese terms.",
                },
                "party": {
                    "type": "string",
                    "description": "Filter by authoring party abbreviation (e.g. PS, PSD, CH, IL, BE, PCP, L).",
                },
                "type": {
                    "type": "string",
                    "description": "Filter by initiative type (e.g. 'Projeto de Lei', 'Proposta de Lei', 'Projeto de Resolução').",
                },
                "legislature": {
                    "type": "string",
                    "description": f"Legislature number in Roman numerals. Defaults to '{DEFAULT_LEGISLATURE}'.",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max results to return (default {DEFAULT_LIMIT}, max {MAX_LIMIT}).",
                },
            },
            "required": ["keywords"],
            "additionalProperties": False,
        },
    },
    "search_votes": {
        "name": "search_votes",
        "description": (
            "Search voting records on legislative initiatives. "
            "Returns vote date, result, and party positions (favor, contra, abstencao). "
            "Can filter by initiative, party position, or vote result."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "initiative_id": {
                    "type": "integer",
                    "description": "Filter by initiative internal ID (from search_initiatives results).",
                },
                "party": {
                    "type": "string",
                    "description": "Find votes where this party voted. Searches favor, contra, and abstencao arrays.",
                },
                "result": {
                    "type": "string",
                    "description": "Filter by vote result (e.g. 'Aprovado', 'Rejeitado').",
                },
                "keywords": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Search keywords to match against the linked initiative title.",
                },
                "legislature": {
                    "type": "string",
                    "description": f"Legislature number. Defaults to '{DEFAULT_LEGISLATURE}'.",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max results (default {DEFAULT_LIMIT}, max {MAX_LIMIT}).",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "search_deputies": {
        "name": "search_deputies",
        "description": (
            "Look up deputies (MPs) in the Portuguese parliament. "
            "Returns name, party, constituency, and situation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Search by deputy name (partial match, case-insensitive).",
                },
                "party": {
                    "type": "string",
                    "description": "Filter by party abbreviation.",
                },
                "legislature": {
                    "type": "string",
                    "description": f"Legislature number. Defaults to '{DEFAULT_LEGISLATURE}'.",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Max results (default {DEFAULT_LIMIT}, max {MAX_LIMIT}).",
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
    "describe_table": {
        "name": "describe_table",
        "description": (
            "Get the column names and types for a parliamentary database table. "
            "Call this BEFORE writing raw_query SQL to avoid column-name errors. "
            "Available tables: iniciativas, iniciativa_events, votes, deputados, "
            "intervencoes, speech_transcripts, iniciativa_autores, iniciativa_comissao, "
            "diplomas, diploma_iniciativas."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table_name": {
                    "type": "string",
                    "description": "Table name to describe.",
                    "enum": sorted(PARLA_TABLES),
                },
            },
            "required": ["table_name"],
            "additionalProperties": False,
        },
    },
    "raw_query": {
        "name": "raw_query",
        "description": (
            "Execute a read-only SQL query against the parliamentary database. "
            "Use this as an escape hatch when the typed search tools don't cover your needs. "
            "IMPORTANT: Call describe_table first to check column names. "
            "Child table FKs point to iniciativas.id (integer), NOT ini_id (string)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "Brief description of what this query looks for (for logging).",
                },
                "sql": {
                    "type": "string",
                    "description": "Read-only SQL query. Must be a SELECT statement.",
                },
            },
            "required": ["description", "sql"],
            "additionalProperties": False,
        },
    },
    "request_gate_review": {
        "name": "request_gate_review",
        "description": (
            "Signal that your research is complete and ready for journalist review. "
            "Call this when you have gathered sufficient data and are ready to present findings. "
            "After calling this tool, you will produce a structured DossierOutput with your research."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "description": "Brief summary of what was found during research (2-3 sentences).",
                },
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
}


# ---------------------------------------------------------------------------
# Tool handlers
# ---------------------------------------------------------------------------

async def _log_query(
    app_db: AsyncSession,
    investigation_id: uuid.UUID,
    stage: str,
    tool_name: str,
    params: dict,
    sql_text: str | None,
    row_count: int | None,
    duration_ms: int | None,
) -> None:
    """Log a tool query to the query_log table."""
    entry = QueryLog(
        investigation_id=investigation_id,
        stage=stage,
        tool_name=tool_name,
        params=params,
        sql_text=sql_text,
        row_count=row_count,
        duration_ms=duration_ms,
    )
    app_db.add(entry)
    await app_db.commit()


def _clamp_limit(limit: int | None) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    return max(1, min(limit, MAX_LIMIT))


async def handle_search_initiatives(
    params: dict,
    parla_db: AsyncSession,
    app_db: AsyncSession,
    investigation_id: uuid.UUID,
    stage: str,
) -> dict:
    keywords = params.get("keywords", [])
    party = params.get("party")
    ini_type = params.get("type")
    legislature = params.get("legislature", DEFAULT_LEGISLATURE)
    limit = _clamp_limit(params.get("limit"))

    conditions = ["i.legislature = :legislature"]
    bind: dict = {"legislature": legislature, "limit": limit}

    if keywords:
        kw_conditions = []
        for idx, kw in enumerate(keywords):
            key = f"kw_{idx}"
            kw_conditions.append(
                f"(LOWER(i.title) LIKE '%%' || LOWER(:{key}) || '%%' "
                f"OR LOWER(COALESCE(i.llm_summary, i.summary, '')) LIKE '%%' || LOWER(:{key}) || '%%')"
            )
            bind[key] = kw
        conditions.append("(" + " OR ".join(kw_conditions) + ")")

    if party:
        conditions.append("LOWER(i.author_name) LIKE '%%' || LOWER(:party) || '%%'")
        bind["party"] = party

    if ini_type:
        conditions.append("LOWER(i.type_description) LIKE '%%' || LOWER(:ini_type) || '%%'")
        bind["ini_type"] = ini_type

    where_clause = " AND ".join(conditions)
    sql = f"""
        SELECT i.id, i.ini_id, i.title, i.type_description, i.author_name,
               i.current_status, i.legislature,
               COALESCE(i.llm_summary, i.summary) AS summary,
               v.resultado AS vote_result, v.favor, v.contra, v.abstencao, v.vote_date
        FROM iniciativas i
        LEFT JOIN LATERAL (
            SELECT resultado, favor, contra, abstencao, vote_date
            FROM votes WHERE iniciativa_id = i.id
            ORDER BY vote_date DESC NULLS LAST LIMIT 1
        ) v ON true
        WHERE {where_clause}
        ORDER BY i.id DESC
        LIMIT :limit
    """

    t0 = time.monotonic()
    result = await parla_db.execute(text(sql), bind)
    rows = result.mappings().all()
    duration_ms = int((time.monotonic() - t0) * 1000)

    data = [
        {
            "id": r["id"],
            "ini_id": r["ini_id"],
            "title": r["title"],
            "type_description": r["type_description"],
            "party": r["author_name"],
            "status": r["current_status"],
            "legislature": r["legislature"],
            "summary": r["summary"],
            "vote_result": r["vote_result"],
            "favor": r["favor"],
            "contra": r["contra"],
            "abstencao": r["abstencao"],
            "vote_date": str(r["vote_date"]) if r["vote_date"] else None,
        }
        for r in rows
    ]

    await _log_query(app_db, investigation_id, stage, "search_initiatives", params, sql, len(data), duration_ms)

    return {"results": data, "count": len(data), "query_description": f"Searched initiatives: {', '.join(keywords)}"}


async def handle_search_votes(
    params: dict,
    parla_db: AsyncSession,
    app_db: AsyncSession,
    investigation_id: uuid.UUID,
    stage: str,
) -> dict:
    initiative_id = params.get("initiative_id")
    party = params.get("party")
    result_filter = params.get("result")
    keywords = params.get("keywords", [])
    legislature = params.get("legislature", DEFAULT_LEGISLATURE)
    limit = _clamp_limit(params.get("limit"))

    conditions = ["i.legislature = :legislature"]
    bind: dict = {"legislature": legislature, "limit": limit}

    if initiative_id:
        conditions.append("v.iniciativa_id = :initiative_id")
        bind["initiative_id"] = initiative_id

    if party:
        conditions.append(
            "(:party = ANY(v.favor) OR :party = ANY(v.contra) OR :party = ANY(v.abstencao))"
        )
        bind["party"] = party

    if result_filter:
        conditions.append("LOWER(v.resultado) = LOWER(:result)")
        bind["result"] = result_filter

    if keywords:
        kw_conditions = []
        for idx, kw in enumerate(keywords):
            key = f"kw_{idx}"
            kw_conditions.append(f"LOWER(i.title) LIKE '%%' || LOWER(:{key}) || '%%'")
            bind[key] = kw
        conditions.append("(" + " OR ".join(kw_conditions) + ")")

    where_clause = " AND ".join(conditions)
    sql = f"""
        SELECT v.id, v.iniciativa_id, i.ini_id, i.title, i.author_name,
               v.phase_name, v.vote_date, v.resultado, v.unanime,
               v.favor, v.contra, v.abstencao, v.detalhe
        FROM votes v
        JOIN iniciativas i ON i.id = v.iniciativa_id
        WHERE {where_clause}
        ORDER BY v.vote_date DESC NULLS LAST
        LIMIT :limit
    """

    t0 = time.monotonic()
    result = await parla_db.execute(text(sql), bind)
    rows = result.mappings().all()
    duration_ms = int((time.monotonic() - t0) * 1000)

    data = [
        {
            "id": r["id"],
            "initiative_id": r["iniciativa_id"],
            "ini_id": r["ini_id"],
            "title": r["title"],
            "party": r["author_name"],
            "phase_name": r["phase_name"],
            "vote_date": str(r["vote_date"]) if r["vote_date"] else None,
            "resultado": r["resultado"],
            "unanime": r["unanime"],
            "favor": r["favor"],
            "contra": r["contra"],
            "abstencao": r["abstencao"],
            "detalhe": r["detalhe"],
        }
        for r in rows
    ]

    await _log_query(app_db, investigation_id, stage, "search_votes", params, sql, len(data), duration_ms)

    return {"results": data, "count": len(data), "query_description": "Searched voting records"}


async def handle_search_deputies(
    params: dict,
    parla_db: AsyncSession,
    app_db: AsyncSession,
    investigation_id: uuid.UUID,
    stage: str,
) -> dict:
    name = params.get("name")
    party = params.get("party")
    legislature = params.get("legislature", DEFAULT_LEGISLATURE)
    limit = _clamp_limit(params.get("limit"))

    conditions = ["d.legislature = :legislature"]
    bind: dict = {"legislature": legislature, "limit": limit}

    if name:
        conditions.append("LOWER(d.name) LIKE '%%' || LOWER(:name) || '%%'")
        bind["name"] = name

    if party:
        conditions.append("LOWER(d.party) = LOWER(:party)")
        bind["party"] = party

    where_clause = " AND ".join(conditions)
    sql = f"""
        SELECT d.id, d.dep_id, d.dep_cad_id, d.name, d.full_name,
               d.party, d.circulo, d.situation, d.legislature
        FROM deputados d
        WHERE {where_clause}
        ORDER BY d.name
        LIMIT :limit
    """

    t0 = time.monotonic()
    result = await parla_db.execute(text(sql), bind)
    rows = result.mappings().all()
    duration_ms = int((time.monotonic() - t0) * 1000)

    data = [
        {
            "id": r["id"],
            "dep_id": r["dep_id"],
            "dep_cad_id": r["dep_cad_id"],
            "name": r["name"],
            "full_name": r["full_name"],
            "party": r["party"],
            "circulo": r["circulo"],
            "situation": r["situation"],
            "legislature": r["legislature"],
        }
        for r in rows
    ]

    await _log_query(app_db, investigation_id, stage, "search_deputies", params, sql, len(data), duration_ms)

    return {"results": data, "count": len(data), "query_description": "Searched deputies"}


async def handle_describe_table(
    params: dict,
    parla_db: AsyncSession,
    app_db: AsyncSession,
    investigation_id: uuid.UUID,
    stage: str,
) -> dict:
    table_name = params.get("table_name", "")
    if table_name not in PARLA_TABLES:
        return {"error": f"Unknown table: {table_name}. Valid: {', '.join(sorted(PARLA_TABLES))}"}

    sql = """
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = :table
        ORDER BY ordinal_position
    """
    t0 = time.monotonic()
    result = await parla_db.execute(text(sql), {"table": table_name})
    rows = result.mappings().all()
    duration_ms = int((time.monotonic() - t0) * 1000)

    columns = [
        {"column": r["column_name"], "type": r["data_type"], "nullable": r["is_nullable"] == "YES"}
        for r in rows
    ]

    await _log_query(app_db, investigation_id, stage, "describe_table", params, sql, len(columns), duration_ms)

    return {"table": table_name, "columns": columns, "count": len(columns)}


async def handle_raw_query(
    params: dict,
    parla_db: AsyncSession,
    app_db: AsyncSession,
    investigation_id: uuid.UUID,
    stage: str,
) -> dict:
    description = params.get("description", "")
    sql_input = params.get("sql", "").strip()

    # Safety: only allow SELECT or WITH (CTE) statements
    stripped_upper = sql_input.strip().upper()
    if not (stripped_upper.startswith("SELECT") or stripped_upper.startswith("WITH")):
        return {"error": "Only SELECT/WITH statements are allowed.", "results": [], "count": 0}

    # Block dangerous patterns using word-boundary regex
    dangerous = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "TRUNCATE", "CREATE", "GRANT"]
    for keyword in dangerous:
        if re.search(rf"\b{keyword}\b", stripped_upper):
            return {"error": f"Statement contains forbidden keyword: {keyword}", "results": [], "count": 0}

    try:
        t0 = time.monotonic()
        result = await parla_db.execute(text(sql_input))
        rows = result.mappings().all()
        duration_ms = int((time.monotonic() - t0) * 1000)

        # Convert rows to JSON-serializable dicts
        data = []
        for r in rows[:MAX_LIMIT]:
            row_dict = {}
            for k, v in r.items():
                if hasattr(v, "isoformat"):
                    row_dict[k] = v.isoformat()
                else:
                    row_dict[k] = v
            data.append(row_dict)

        await _log_query(app_db, investigation_id, stage, "raw_query", {"description": description}, sql_input, len(data), duration_ms)

        return {"results": data, "count": len(data), "query_description": description}

    except Exception as e:
        logger.warning("raw_query failed: %s", e)
        await _log_query(app_db, investigation_id, stage, "raw_query", {"description": description, "error": str(e)}, sql_input, 0, None)
        return {"error": str(e), "results": [], "count": 0}


async def handle_request_gate_review(
    params: dict,
    **kwargs,
) -> dict:
    """Returns confirmation to agent. The actual gate triggering is handled by ws.py."""
    summary = params.get("summary", "")
    return {
        "status": "gate_review_requested",
        "message": (
            "Research review has been requested. You must now produce your final structured "
            "research dossier. The system will guide you to output a DossierOutput with: "
            "executive_summary, topic_keywords, initiatives, patterns, voting_summary, "
            "data_gaps, and recommended_next_steps."
        ),
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# Tool registry builder
# ---------------------------------------------------------------------------

def build_tool_registry(
    parla_session_factory: async_sessionmaker,
    app_db: AsyncSession,
    investigation_id: uuid.UUID,
    stage: str,
) -> tuple[list[dict], dict[str, callable]]:
    """Build tool definitions and handlers for the agent.

    Returns (tool_definitions, tool_handlers) where tool_handlers maps
    tool name -> async callable(params) -> dict.

    Each DB tool opens its own parla_db session, so a SQL error in one
    call never poisons subsequent calls (session-per-tool-call pattern).
    """
    tool_defs = [
        TOOL_DEFINITIONS["search_initiatives"],
        TOOL_DEFINITIONS["search_votes"],
        TOOL_DEFINITIONS["search_deputies"],
        TOOL_DEFINITIONS["describe_table"],
        TOOL_DEFINITIONS["raw_query"],
        TOOL_DEFINITIONS["request_gate_review"],
    ]

    async def _search_initiatives(params: dict) -> dict:
        async with parla_session_factory() as parla_db:
            return await handle_search_initiatives(params, parla_db, app_db, investigation_id, stage)

    async def _search_votes(params: dict) -> dict:
        async with parla_session_factory() as parla_db:
            return await handle_search_votes(params, parla_db, app_db, investigation_id, stage)

    async def _search_deputies(params: dict) -> dict:
        async with parla_session_factory() as parla_db:
            return await handle_search_deputies(params, parla_db, app_db, investigation_id, stage)

    async def _describe_table(params: dict) -> dict:
        async with parla_session_factory() as parla_db:
            return await handle_describe_table(params, parla_db, app_db, investigation_id, stage)

    async def _raw_query(params: dict) -> dict:
        async with parla_session_factory() as parla_db:
            return await handle_raw_query(params, parla_db, app_db, investigation_id, stage)

    async def _request_gate_review(params: dict) -> dict:
        return await handle_request_gate_review(params)

    handlers = {
        "search_initiatives": _search_initiatives,
        "search_votes": _search_votes,
        "search_deputies": _search_deputies,
        "describe_table": _describe_table,
        "raw_query": _raw_query,
        "request_gate_review": _request_gate_review,
    }

    return tool_defs, handlers


def build_research_tool_registry(
    parla_session_factory: async_sessionmaker,
    app_db: AsyncSession,
    investigation_id: uuid.UUID,
    stage: str,
) -> tuple[list[dict], dict[str, callable]]:
    """Build minimal tool registry for research revision (escape hatch only).

    Only includes raw_query, describe_table, and request_gate_review.
    The typed search tools (search_initiatives, search_votes, search_deputies)
    are excluded because pre-fetched data is already in the system prompt.
    """
    tool_defs = [
        TOOL_DEFINITIONS["describe_table"],
        TOOL_DEFINITIONS["raw_query"],
        TOOL_DEFINITIONS["request_gate_review"],
    ]

    async def _describe_table(params: dict) -> dict:
        async with parla_session_factory() as parla_db:
            return await handle_describe_table(params, parla_db, app_db, investigation_id, stage)

    async def _raw_query(params: dict) -> dict:
        async with parla_session_factory() as parla_db:
            return await handle_raw_query(params, parla_db, app_db, investigation_id, stage)

    async def _request_gate_review(params: dict) -> dict:
        return await handle_request_gate_review(params)

    handlers = {
        "describe_table": _describe_table,
        "raw_query": _raw_query,
        "request_gate_review": _request_gate_review,
    }

    return tool_defs, handlers
