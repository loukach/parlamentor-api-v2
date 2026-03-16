# CLAUDE.md

## Commands

```bash
python -m uvicorn api.main:app --reload --port 8000   # Dev server
alembic upgrade head                                    # Run migrations
alembic revision -m "description"                       # New migration (no autogenerate without local DB)
python scripts/seed.py                                  # Seed test data
python scripts/validate_architecture.py                 # Run 5 validation tests
ruff check .                                            # Lint
```

## Architecture

FastAPI + SQLAlchemy async + asyncpg. Dual database pattern:
- **App DB** (`parlamentor-db`): Read-write. Investigations, stages, outputs, logs.
- **Parla! DB** (`viriato-postgres`): Read-only. Parliamentary data (initiatives, votes, speeches).

Pydantic Settings for config. Alembic for migrations (PostgreSQL only, no SQLite). Langfuse SDK v3 for observability.

## Key Files

| File | Purpose |
|------|---------|
| `api/config.py` | Pydantic Settings, URL normalization |
| `api/db.py` | Dual async engines + session factories (Parla pool: `pool_pre_ping`, `pool_recycle=300`) |
| `api/main.py` | FastAPI app, lifespan, CORS, health endpoint, WS router |
| `api/models/__init__.py` | 9 SQLAlchemy models (Investigation, Stage, StageOutput, StateSnapshot, GateLog, AgentLog, Message, QueryLog, ResearchAssets) |
| `api/routes.py` | REST endpoints (CRUD investigations, stages, messages, stage outputs, research assets) |
| `api/schemas.py` | Pydantic request/response schemas |
| `api/orchestrator.py` | State machine (3 stages), gate logic (approve/revise/reject), cost logging |
| `api/executor.py` | Agent loop (async generator), streaming, tool dispatch, structured extraction, server tool support |
| `api/prefetch.py` | Pre-fetch pipeline: Haiku keyword expansion + batch SQL (initiatives, votes, diplomas) |
| `api/research.py` | Research analyst config: system prompt, DossierOutput schema (with diplomas, media_signals) |
| `api/analysis.py` | Analysis config: merged findings + story angles, AnalysisOutput schema |
| `api/drafting.py` | Drafting config: Opus skill mode, DraftOutput schema, chat-based iteration |
| `api/tools.py` | DB tools + registries: `build_tool_registry` (all 6), `build_research_tool_registry` (escape hatch: raw_query, describe_table, request_gate_review) |
| `api/ws.py` | WebSocket endpoint `/ws/chat/{id}`: message handling, gate decisions, prefetch, 3 stage handlers |
| `api/costs.py` | Anthropic pricing table, cost calculation with cache pricing |
| `api/tracing.py` | Langfuse TraceContext: span/generation/tool call logging |
| `scripts/validate_architecture.py` | 5 validation tests (structured output, caching, Langfuse, extraction, tools) |
| `scripts/seed.py` | Test data seeder |

## Conventions

- **PostgreSQL only.** No SQLite fallbacks. Alembic migrations target PostgreSQL (no `render_as_batch`).
- **Async everywhere.** All DB access via `AsyncSession`. FastAPI deps yield sessions.
- **UUID primary keys.** All tables use `uuid4` defaults.
- **JSONB for structured data.** Stage outputs, state snapshots, tool inputs/outputs.
- **Langfuse SDK v3 direct.** `from langfuse import Langfuse`. Use `start_span()` and `start_observation(as_type='generation')`. NOT the OTEL path.
- **Anthropic structured output.** Use `output_config={"format": {"type": "json_schema", "schema": {...}}}`. Compatible with `thinking`.
- **Cost calculation.** Backend-computed from token counts (Langfuse has cache token double-counting bug).
- **Session-per-tool-call.** Each DB tool opens its own Parla session via factory (`async with parla_session_factory() as session`). Prevents SQL errors from poisoning subsequent queries on a shared transaction.

## Database

Migrations run against Render `parlamentor-db` directly (external access enabled, IP `0.0.0.0/0`). No local PostgreSQL needed.

```bash
# Run migration against remote DB
alembic upgrade head

# Create new migration (hand-write the upgrade/downgrade)
alembic revision -m "description"
```

## Validation Results (Phase 0.7)

All 5 architectural assumptions confirmed (2026-03-08):
- A: Structured output + extended thinking work together
- B: Prompt caching works (>= 1024 tokens required)
- C: Langfuse SDK v3 integration works
- D: Agent extraction pattern (multi-turn + structured output extraction) works
- E: Strict tool schemas respected
