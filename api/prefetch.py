"""Pre-fetch pipeline: semantic search + hydration via Viriato API.

All parliamentary data accessed through Viriato API — no direct DB connection.

1. semantic_search(topic) — embedding-based initiative discovery
2. hydrate_via_api(ini_ids) — batch fetch full details (initiatives, votes, diplomas)
"""

import logging
import time

import httpx

from api.config import settings

logger = logging.getLogger(__name__)

VIRIATO_SEARCH_TIMEOUT = 8.0  # seconds — can be slow on cold start
VIRIATO_HYDRATE_TIMEOUT = 10.0


async def semantic_search(topic: str) -> list[str]:
    """Call Viriato /api/search for embedding-based initiative discovery.

    Returns deduplicated list of ini_ids. Falls back to empty list on error.
    """
    api_url = settings.viriato_api_url.rstrip("/")
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=VIRIATO_SEARCH_TIMEOUT) as client:
            resp = await client.get(
                f"{api_url}/api/search",
                params={"q": topic, "limit": 20},
            )
            resp.raise_for_status()
            data = resp.json()
    except Exception:
        logger.warning("Semantic search failed for topic: %s", topic, exc_info=True)
        return []

    duration = int((time.monotonic() - t0) * 1000)

    # Extract ini_ids from temas > matchingInitiatives
    seen = set()
    ini_ids = []
    for tema in data.get("results", {}).get("temas", []):
        for ini in tema.get("matchingInitiatives", []):
            ini_id = ini.get("iniId")
            if ini_id and ini_id not in seen:
                seen.add(ini_id)
                ini_ids.append(ini_id)

    logger.info("Semantic search: %d ini_ids in %dms", len(ini_ids), duration)
    return ini_ids


async def hydrate_via_api(
    ini_ids: list[str],
) -> tuple[list[dict], list[dict], list[dict]]:
    """Batch fetch full initiative details from Viriato API.

    Calls GET /api/v3/iniciativas/batch?ids=... and maps the V3 response
    to the shapes expected by research_assets table and build_research_prompt.

    Returns (initiatives, votes, diplomas). Falls back to empty lists on error.
    """
    if not ini_ids:
        return [], [], []

    api_url = settings.viriato_api_url.rstrip("/")
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=VIRIATO_HYDRATE_TIMEOUT) as client:
            resp = await client.get(
                f"{api_url}/api/v3/iniciativas/batch",
                params={"ids": ",".join(ini_ids)},
            )
            resp.raise_for_status()
            items = resp.json()
    except Exception:
        logger.warning("Hydration failed for %d ini_ids", len(ini_ids), exc_info=True)
        return [], [], []

    duration = int((time.monotonic() - t0) * 1000)

    initiatives = []
    votes = []
    diplomas = []

    for item in items:
        ini_id = item.get("iniId", "")

        # Map to research_assets initiative shape
        vote = item.get("latestVote") or {}
        initiatives.append({
            "ini_id": ini_id,
            "title": item.get("title", ""),
            "type_description": item.get("typeDescription", ""),
            "party": (item.get("parties") or [""])[0],
            "status": item.get("citizenStatus", {}).get("statusLabel", ""),
            "legislature": "",  # Not in V3 response — acceptable, agent has context
            "summary": item.get("summary"),
            "vote_result": vote.get("result"),
            "favor": vote.get("favor", []),
            "contra": vote.get("contra", []),
            "abstencao": vote.get("abstencao", []),
            "vote_date": vote.get("date"),
            "citizen_status": item.get("citizenStatus"),
        })

        # Map vote to votes list (for prompt context)
        if vote:
            votes.append({
                "ini_id": ini_id,
                "title": item.get("title", ""),
                "party": (item.get("parties") or [""])[0],
                "phase_name": vote.get("phase", ""),
                "vote_date": vote.get("date"),
                "resultado": vote.get("result", ""),
                "favor": vote.get("favor", []),
                "contra": vote.get("contra", []),
                "abstencao": vote.get("abstencao", []),
            })

        # Map diplomas
        for d in item.get("diplomas", []):
            diplomas.append({
                "tipo": d.get("tipo", ""),
                "numero": d.get("numero"),
                "titulo": d.get("titulo"),
                "pub_date": d.get("pubDate"),
                "ini_id": ini_id,
            })

    logger.info(
        "Hydration: %d initiatives, %d votes, %d diplomas in %dms",
        len(initiatives), len(votes), len(diplomas), duration,
    )
    return initiatives, votes, diplomas
