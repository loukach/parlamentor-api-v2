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
| `api/db.py` | Dual async engines + session factories |
| `api/main.py` | FastAPI app, lifespan, CORS, health endpoint |
| `api/models/__init__.py` | 8 SQLAlchemy models (Investigation, Stage, StageOutput, StateSnapshot, GateLog, AgentLog, Message, QueryLog) |
| `api/routes.py` | REST endpoints (CRUD investigations, stage outputs) |
| `api/schemas.py` | Pydantic request/response schemas |
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
