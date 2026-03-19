"""WebSocket endpoint for real-time agent chat.

Endpoint: GET /ws/chat/{investigation_id}

Protocol:
  Client -> Server: message, gate_decision, cancel
  Server -> Client: connected, text_delta, thinking, tool_call, tool_result,
                    usage, stage_output, gate_ready, gate_result,
                    stage_started, assets_updated, turn_complete, error
"""

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.auth import validate_ws_token
from api.config import DEFAULT_MODEL
from api.db import app_session_factory, parla_session_factory
from api.executor import run_agent
from api.models import Investigation, Message, ResearchAssets
from api.orchestrator import (
    get_state,
    log_api_call,
    process_gate,
    save_output,
    start_stage,
)
from api.prefetch import batch_fetch, expand_keywords
from api.research import (
    DOSSIER_SCHEMA,
    build_research_prompt,
)
from api.analysis import (
    ANALYSIS_SCHEMA,
    ANALYSIS_MODEL,
    ANALYSIS_THINKING,
    build_analysis_prompt,
)
from api.drafting import (
    DRAFT_SCHEMA,
    DRAFTING_MODEL,
    DRAFTING_THINKING,
    build_drafting_prompt,
)
from api.tools import build_research_tool_registry
from api.tracing import TraceContext

from sqlalchemy import select, text

logger = logging.getLogger(__name__)

ws_router = APIRouter()


@ws_router.websocket("/ws/chat/{investigation_id}")
async def websocket_chat(websocket: WebSocket, investigation_id: uuid.UUID):
    await websocket.accept()

    # Validate JWT token from query params
    token = websocket.query_params.get("token")
    if not token:
        await websocket.close(code=4001, reason="Missing authentication token")
        return

    try:
        user_id = validate_ws_token(token)
    except ValueError as e:
        await websocket.close(code=4003, reason=str(e))
        return

    # Verify investigation belongs to user
    async with app_session_factory() as db:
        result = await db.execute(select(Investigation).where(Investigation.id == investigation_id))
        investigation = result.scalar()
        if not investigation:
            await websocket.close(code=4004, reason="Investigation not found")
            return
        if investigation.user_id is not None and investigation.user_id != user_id:
            await websocket.close(code=4003, reason="Access denied")
            return

    # Agent task handle (for cancellation)
    agent_task: asyncio.Task | None = None
    cancel_event = asyncio.Event()
    timeout_task: asyncio.Task | None = None

    async def _timeout_watchdog(task: asyncio.Task):
        """Cancel agent task after 10 minutes."""
        try:
            await asyncio.sleep(600)
            if not task.done():
                logger.error("Agent task timed out for %s", investigation_id)
                cancel_event.set()
                await _send(websocket, {"type": "error", "content": "O agente excedeu o tempo limite."})
                await _send(websocket, {"type": "turn_complete"})
                task.cancel()
        except asyncio.CancelledError:
            pass  # Watchdog cancelled (agent finished or new cancel)

    async def _cancel_agent():
        """Cancel a running agent task and wait for it to finish."""
        nonlocal agent_task, timeout_task
        if timeout_task and not timeout_task.done():
            timeout_task.cancel()
            timeout_task = None
        cancel_event.set()
        if agent_task and not agent_task.done():
            agent_task.cancel()
            try:
                await agent_task
            except (asyncio.CancelledError, Exception):
                pass
        agent_task = None

    try:
        # Send connected message with current state
        async with app_session_factory() as db:
            try:
                state = await get_state(db, investigation_id)
            except ValueError:
                await _send(websocket, {"type": "error", "content": "Investigation not found"})
                await websocket.close()
                return

        await _send(websocket, {
            "type": "connected",
            "investigation_id": str(investigation_id),
            "current_stage": state["investigation"]["current_stage"],
        })

        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            msg_type = msg.get("type", "message")

            if msg_type == "cancel":
                was_running = agent_task and not agent_task.done()
                await _cancel_agent()
                if was_running:
                    await _send(websocket, {"type": "turn_complete"})
                cancel_event.clear()

            elif msg_type == "gate_decision":
                await _cancel_agent()
                cancel_event.clear()
                gate_result = await _handle_gate_decision(
                    websocket, investigation_id, msg
                )

                # Auto-start drafting after analysis approval (angle selected)
                if gate_result and gate_result.get("next_stage") == "drafting":
                    next_stage = "drafting"
                    kickoff = "Iniciar fase: drafting"

                    try:
                        # Send stage_started
                        await _send(websocket, {
                            "type": "stage_started",
                            "stage": next_stage,
                        })

                        cancel_event.clear()
                        agent_task = asyncio.create_task(
                            _handle_message(websocket, investigation_id, kickoff, DEFAULT_MODEL, cancel_event)
                        )
                        timeout_task = asyncio.create_task(_timeout_watchdog(agent_task))

                    except Exception:
                        logger.exception("Drafting auto-start failed for %s", investigation_id)
                        await _send(websocket, {
                            "type": "error",
                            "content": "A fase de rascunho falhou. Envie uma mensagem para tentar novamente."
                        })

            else:
                # Default: treat as user message
                content = msg.get("content", "")
                model = msg.get("model", DEFAULT_MODEL)

                if not content.strip():
                    continue

                # Cancel any running agent before starting new turn
                if agent_task and not agent_task.done():
                    await _cancel_agent()

                # Reset cancel event for new turn
                cancel_event.clear()

                # Run agent as background task (message loop stays free for cancel)
                agent_task = asyncio.create_task(
                    _handle_message(
                        websocket, investigation_id, content, model, cancel_event
                    )
                )
                timeout_task = asyncio.create_task(
                    _timeout_watchdog(agent_task)
                )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: %s", investigation_id)
        cancel_event.set()
        if timeout_task and not timeout_task.done():
            timeout_task.cancel()
        if agent_task and not agent_task.done():
            agent_task.cancel()
    except Exception:
        logger.exception("WebSocket error for %s", investigation_id)
        cancel_event.set()
        try:
            await _send(websocket, {"type": "error", "content": "Internal server error"})
        except Exception:
            pass


async def _stream_agent_events(
    websocket: WebSocket,
    agent_generator,
    gate_requested_at_start: bool = False,
) -> tuple[dict | None, dict, str]:
    """Stream agent events to websocket, return (structured_output, total_usage, assistant_text).

    Args:
        websocket: WebSocket connection
        agent_generator: AsyncGenerator from run_agent()
        gate_requested_at_start: True if skill_mode (no tool calls expected)

    Returns:
        (structured_output_dict, total_usage_dict, assistant_text_str)
    """
    gate_requested = gate_requested_at_start
    assistant_text = ""
    structured_output = None
    total_usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_create_tokens": 0,
        "cost_usd": 0.0,
        "tool_calls_count": 0,
        "iteration_count": 0,
    }

    async for event in agent_generator:
        event_type = event.get("type")

        if event_type == "text_delta":
            if not gate_requested:
                # Only stream prose to chat; structured JSON after gate goes to stage_output
                assistant_text += event.get("content", "")
                await _send(websocket, event)

        elif event_type == "thinking":
            await _send(websocket, event)

        elif event_type == "tool_call":
            total_usage["tool_calls_count"] += 1
            await _send(websocket, {
                "type": "tool_call",
                "tool": event.get("tool"),
                "summary": event.get("summary"),
            })

        elif event_type == "tool_result":
            if event.get("gate_requested"):
                gate_requested = True
            await _send(websocket, {
                "type": "tool_result",
                "tool": event.get("tool"),
                "summary": event.get("summary"),
                "row_count": event.get("row_count"),
            })

        elif event_type == "usage":
            total_usage["input_tokens"] += event.get("input_tokens", 0)
            total_usage["output_tokens"] += event.get("output_tokens", 0)
            total_usage["cache_read_tokens"] += event.get("cache_read_tokens", 0)
            total_usage["cache_create_tokens"] += event.get("cache_create_tokens", 0)
            total_usage["cost_usd"] += event.get("cost_usd", 0.0)
            total_usage["iteration_count"] = event.get("iteration", 0)
            await _send(websocket, {
                "type": "usage",
                "input_tokens": total_usage["input_tokens"],
                "output_tokens": total_usage["output_tokens"],
                "cache_read_tokens": total_usage["cache_read_tokens"],
                "cache_create_tokens": total_usage["cache_create_tokens"],
                "cost_usd": total_usage["cost_usd"],
                "iteration": event.get("iteration", 0),
            })

        elif event_type == "error":
            await _send(websocket, event)

        elif event_type == "structured_output":
            structured_output = event.get("output")

        elif event_type == "agent_done":
            pass  # Will be handled by caller

    return structured_output, total_usage, assistant_text


async def _run_analysis_stage(
    websocket: WebSocket,
    investigation_id: uuid.UUID,
    model: str,
    cancel_event: asyncio.Event,
    trace: TraceContext,
) -> tuple[dict, str]:
    """Run analysis stage handler. Returns (total_usage, trace_output)."""
    # Load research output
    async with app_session_factory() as app_db:
        state = await get_state(app_db, investigation_id)
        dossier_output = state["outputs"].get("research", {}).get("output_data", {})

        # Check for revision feedback
        feedback = await _get_latest_feedback(investigation_id, "analysis")

    # Build prompt
    system_prompt = await build_analysis_prompt(dossier_output, feedback)

    # Run agent in skill mode (single call, no tools)
    async with app_session_factory() as app_db:
        agent_gen = run_agent(
            model=ANALYSIS_MODEL,
            system_prompt=system_prompt,
            messages=await _load_conversation(app_db, investigation_id, "analysis"),
            tools=[],
            tool_handlers={},
            thinking=ANALYSIS_THINKING,
            output_schema=ANALYSIS_SCHEMA["schema"],
            cancel_event=cancel_event,
            trace=trace,
            skill_mode=True,
        )

        structured_output, total_usage, assistant_text = await _stream_agent_events(
            websocket, agent_gen, gate_requested_at_start=True
        )

        if structured_output:
            # Save output to DB
            await save_output(app_db, investigation_id, "analysis", structured_output)

            # Send stage output to client
            await _send(websocket, {
                "type": "stage_output",
                "stage": "analysis",
                "output": structured_output,
            })

            # Send gate_ready
            summary = structured_output.get("executive_summary", "")[:200]
            await _send(websocket, {
                "type": "gate_ready",
                "stage": "analysis",
                "summary": summary,
            })

            trace_output = structured_output.get("executive_summary", "")
            # Save executive summary as assistant message for chat history
            assistant_text = structured_output.get("executive_summary", "")
        else:
            trace_output = assistant_text

        # Persist assistant message
        if assistant_text.strip():
            async with app_session_factory() as app_db2:
                assistant_msg = Message(
                    investigation_id=investigation_id,
                    stage="analysis",
                    role="assistant",
                    content=assistant_text,
                    metadata_={"model": model, "usage": total_usage},
                )
                app_db2.add(assistant_msg)
                await app_db2.commit()

        # Log API call
        async with app_session_factory() as app_db2:
            await log_api_call(
                app_db2,
                investigation_id,
                "analysis",
                model,
                **{k: v for k, v in total_usage.items() if k != "cost_usd"},
            )

    return total_usage, trace_output


async def _run_drafting_stage(
    websocket: WebSocket,
    investigation_id: uuid.UUID,
    user_feedback: str,
    cancel_event: asyncio.Event,
    trace: TraceContext,
) -> tuple[dict, str]:
    """Run drafting stage handler (skill mode, iterative). Returns (total_usage, trace_output)."""
    # Load selected angle from analysis gate
    async with app_session_factory() as app_db:
        from api.models import GateLog
        result = await app_db.execute(
            select(GateLog)
            .where(
                GateLog.investigation_id == investigation_id,
                GateLog.stage == "analysis",
                GateLog.action == "approve",
            )
            .order_by(GateLog.created_at.desc())
            .limit(1)
        )
        gate = result.scalar()

        if not gate or not gate.rationale:
            await _send(websocket, {
                "type": "error",
                "content": "No angle selected from analysis gate. Cannot proceed to drafting."
            })
            return {}, ""

        import json as json_module
        metadata = json_module.loads(gate.rationale)
        # Handle double-serialized JSON (string instead of dict)
        if isinstance(metadata, str):
            metadata = json_module.loads(metadata)
        selected_angle_id = metadata.get("selected_angle_id")

        # Load analysis output and find selected angle
        state = await get_state(app_db, investigation_id)
        analysis_output = state["outputs"].get("analysis", {}).get("output_data", {})
        all_angles = analysis_output.get("story_angles", [])
        angle = next((a for a in all_angles if a.get("id") == selected_angle_id), None)

        if not angle:
            # Fallback: use first angle or journalist's custom angle
            angle = all_angles[0] if all_angles else {"thesis": user_feedback, "type": "explainer", "outline": [], "key_findings": [], "source_gaps": []}

        # Load research dossier
        dossier_output = state["outputs"].get("research", {}).get("output_data", {})

        # Load previous draft (if any)
        latest_draft = state["outputs"].get("drafting", {}).get("output_data")

        # Determine feedback: first run uses kickoff message, revisions use journalist feedback
        is_first_draft = latest_draft is None
        feedback = None if is_first_draft else user_feedback

    # Build prompt
    system_prompt = await build_drafting_prompt(
        angle=angle,
        dossier_output=dossier_output,
        analysis_output=analysis_output,
        previous_draft=latest_draft,
        feedback=feedback,
    )

    # Run Opus in skill mode (no tools, single call)
    async with app_session_factory() as app_db:
        agent_gen = run_agent(
            model=DRAFTING_MODEL,
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": "Write the article." if is_first_draft else user_feedback}],
            tools=[],
            tool_handlers={},
            thinking=DRAFTING_THINKING,
            output_schema=DRAFT_SCHEMA["schema"],
            cancel_event=cancel_event,
            trace=trace,
            skill_mode=True,
        )

        structured_output, total_usage, assistant_text = await _stream_agent_events(
            websocket, agent_gen, gate_requested_at_start=True
        )

        if structured_output:
            # Save output WITHOUT gate_pending (chat-based iteration)
            await save_output(
                app_db, investigation_id, "drafting", structured_output,
                set_gate_pending=False,
            )

            await _send(websocket, {
                "type": "stage_output",
                "stage": "drafting",
                "output": structured_output,
            })

            # Stream completion message to chat (skill mode suppresses text_delta)
            word_count = structured_output.get("word_count", 0)
            completion_msg = (
                f"O rascunho esta pronto ({word_count} palavras). "
                "Reveja o artigo no separador Rascunho e envie feedback para ajustes."
            )
            await _send(websocket, {"type": "text_delta", "content": completion_msg})
            assistant_text = completion_msg

            trace_output = structured_output.get("title", "")
        else:
            trace_output = assistant_text

        # Persist assistant message
        if assistant_text.strip():
            async with app_session_factory() as app_db2:
                assistant_msg = Message(
                    investigation_id=investigation_id,
                    stage="drafting",
                    role="assistant",
                    content=assistant_text,
                    metadata_={"model": DRAFTING_MODEL, "usage": total_usage},
                )
                app_db2.add(assistant_msg)
                await app_db2.commit()

        # Log API call
        async with app_session_factory() as app_db2:
            await log_api_call(
                app_db2,
                investigation_id,
                "drafting",
                DRAFTING_MODEL,
                **{k: v for k, v in total_usage.items() if k != "cost_usd"},
            )

    return total_usage, trace_output


async def _handle_message(
    websocket: WebSocket,
    investigation_id: uuid.UUID,
    content: str,
    model: str,
    cancel_event: asyncio.Event,
) -> None:
    """Handle a user message: persist, build prompt, run agent, stream results."""
    # Get current stage first for TraceContext
    async with app_session_factory() as app_db_pre:
        state_pre = await get_state(app_db_pre, investigation_id)
        current_stage_pre = state_pre["investigation"]["current_stage"]

    trace = TraceContext(str(investigation_id), current_stage_pre, content)
    assistant_text = ""
    trace_output = ""

    try:
        async with app_session_factory() as app_db:
            # Get current stage
            state = await get_state(app_db, investigation_id)
            current_stage = state["investigation"]["current_stage"]

            # Ensure stage is active
            stage_info = next(
                (s for s in state["stages"] if s["stage"] == current_stage), None
            )
            if stage_info and stage_info["status"] == "pending":
                await start_stage(app_db, investigation_id, current_stage)
            elif stage_info and stage_info["status"] == "gate_pending":
                # User sent a message during gate review (research iteration)
                from sqlalchemy import update as sql_update
                from api.models import Stage
                await app_db.execute(
                    sql_update(Stage)
                    .where(Stage.investigation_id == investigation_id, Stage.stage == current_stage)
                    .values(status="active")
                )
                await app_db.commit()

            # Persist user message
            user_msg = Message(
                investigation_id=investigation_id,
                stage=current_stage,
                role="user",
                content=content,
            )
            app_db.add(user_msg)
            await app_db.commit()

            # Load conversation history from DB
            messages = await _load_conversation(app_db, investigation_id, current_stage)

        # Route to stage handler
        if current_stage == "research":
            topic = state["investigation"]["topic"]
            feedback = await _get_latest_feedback(investigation_id, current_stage)

            # Check if we already have research output (revision flow)
            existing_output = state["outputs"].get("research")
            is_first_run = existing_output is None

            # Step 1: Pre-fetch data (first run or explicit re-fetch request)
            prefetch_data = None
            kw_usage = None
            if is_first_run:
                # Expand keywords with Haiku
                await _send(websocket, {
                    "type": "tool_call",
                    "tool": "keyword_expansion",
                    "summary": f"A expandir palavras-chave: {topic}",
                })
                keywords, kw_usage = await expand_keywords(topic)
                await _send(websocket, {
                    "type": "tool_result",
                    "tool": "keyword_expansion",
                    "summary": f"{len(keywords)} palavras-chave geradas",
                    "row_count": len(keywords),
                })

                # Log keyword expansion cost
                if kw_usage:
                    await _send(websocket, {
                        "type": "usage",
                        "input_tokens": kw_usage["input_tokens"],
                        "output_tokens": kw_usage["output_tokens"],
                        "cache_read_tokens": 0,
                        "cache_create_tokens": 0,
                        "cost_usd": kw_usage["cost_usd"],
                        "iteration": 0,
                    })

                if cancel_event.is_set():
                    return

                # Batch SQL fetch
                await _send(websocket, {
                    "type": "tool_call",
                    "tool": "batch_fetch",
                    "summary": "A recolher dados parlamentares",
                })
                prefetch_data = await batch_fetch(parla_session_factory, topic, keywords)
                stats = prefetch_data["stats"]
                await _send(websocket, {
                    "type": "tool_result",
                    "tool": "batch_fetch",
                    "summary": (
                        f"{stats['initiative_count']} iniciativas, "
                        f"{stats['vote_count']} votacoes, "
                        f"{stats['diploma_count']} diplomas"
                    ),
                    "row_count": stats["initiative_count"] + stats["vote_count"] + stats["diploma_count"],
                })

                if cancel_event.is_set():
                    return

                # Hydrate research assets for panel display (immediate)
                ini_ids = [r["ini_id"] for r in prefetch_data["initiatives"] if r.get("ini_id")]
                if ini_ids:
                    async with parla_session_factory() as parla_db:
                        hydrated_inis, hydrated_votes = await _hydrate_assets(parla_db, ini_ids)
                    async with app_session_factory() as app_db2:
                        await _upsert_research_assets(
                            app_db2, investigation_id, hydrated_inis, hydrated_votes
                        )
                    # Notify frontend to refresh assets panel immediately
                    await _send(websocket, {"type": "assets_updated"})

            # Step 2: Build prompt with pre-fetched data
            system_prompt = await build_research_prompt(
                topic, prefetch_data=prefetch_data, feedback=feedback
            )

            # Step 3: Run Sonnet analysis
            async with app_session_factory() as app_db:
                if is_first_run:
                    # Skill mode — single call, no tools. Pre-fetched data in prompt.
                    agent_gen = run_agent(
                        model=model,
                        system_prompt=system_prompt,
                        messages=messages,
                        tools=[],
                        tool_handlers={},
                        thinking={"type": "enabled", "budget_tokens": 10000},
                        output_schema=DOSSIER_SCHEMA["schema"],
                        cancel_event=cancel_event,
                        trace=trace,
                        skill_mode=True,
                    )
                    structured_output, total_usage, assistant_prose = await _stream_agent_events(
                        websocket, agent_gen, gate_requested_at_start=True
                    )
                    # Save executive summary as assistant message for revision context
                    if structured_output:
                        assistant_prose = structured_output.get("executive_summary", "")
                else:
                    # Revision mode — escape hatch tools + web search
                    tool_defs, tool_handlers = build_research_tool_registry(
                        parla_session_factory, app_db, investigation_id, current_stage
                    )
                    web_search_tool = {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": 3,
                    }
                    agent_gen = run_agent(
                        model=model,
                        system_prompt=system_prompt,
                        messages=messages,
                        tools=tool_defs,
                        tool_handlers=tool_handlers,
                        thinking={"type": "enabled", "budget_tokens": 10000},
                        output_schema=DOSSIER_SCHEMA["schema"],
                        cancel_event=cancel_event,
                        trace=trace,
                        server_tools=[web_search_tool],
                    )
                    structured_output, total_usage, assistant_prose = await _stream_agent_events(
                        websocket, agent_gen, gate_requested_at_start=False
                    )

                # Add keyword expansion usage to totals
                if kw_usage:
                    total_usage["input_tokens"] += kw_usage["input_tokens"]
                    total_usage["output_tokens"] += kw_usage["output_tokens"]
                    total_usage["cost_usd"] += kw_usage["cost_usd"]

                if structured_output:
                    await save_output(app_db, investigation_id, current_stage, structured_output)

                    # Update research assets from curated ini_ids
                    curated_ini_ids = [
                        ini["ini_id"]
                        for ini in structured_output.get("initiatives", [])
                        if ini.get("ini_id")
                    ]
                    if curated_ini_ids:
                        async with parla_session_factory() as parla_db:
                            inis, vts = await _hydrate_assets(parla_db, curated_ini_ids)
                        async with app_session_factory() as app_db2:
                            await _upsert_research_assets(
                                app_db2, investigation_id, inis, vts
                            )
                        await _send(websocket, {"type": "assets_updated"})

                    await _send(websocket, {
                        "type": "stage_output",
                        "stage": current_stage,
                        "output": structured_output,
                    })

                    # Gate: journalist reviews research before advancing
                    summary = structured_output.get("executive_summary", "")[:200]
                    await _send(websocket, {
                        "type": "gate_ready",
                        "stage": current_stage,
                        "summary": summary,
                    })

                    # Format assistant message with markdown
                    exec_summary = structured_output.get("executive_summary", "")
                    assistant_prose = (
                        exec_summary
                        + "\n\n---\n\n"
                        + "**Próximos passos:** Reveja o dossier completo no painel à direita. "
                        + "Pode refinar a pesquisa enviando uma mensagem, "
                        + "ou avançar para a análise editorial."
                    )
                    await _send(websocket, {
                        "type": "text_delta",
                        "content": assistant_prose,
                    })

                    trace_output = exec_summary
                else:
                    trace_output = assistant_prose

                assistant_text = assistant_prose

                if assistant_text.strip():
                    async with app_session_factory() as app_db2:
                        assistant_msg = Message(
                            investigation_id=investigation_id,
                            stage=current_stage,
                            role="assistant",
                            content=assistant_text,
                            metadata_={"model": model, "usage": total_usage},
                        )
                        app_db2.add(assistant_msg)
                        await app_db2.commit()

                async with app_session_factory() as app_db2:
                    await log_api_call(
                        app_db2,
                        investigation_id,
                        current_stage,
                        model,
                        **{k: v for k, v in total_usage.items() if k != "cost_usd"},
                    )

        elif current_stage == "analysis":
            total_usage, trace_out = await _run_analysis_stage(
                websocket, investigation_id, model, cancel_event, trace
            )
            trace_output = trace_out

        elif current_stage == "drafting":
            total_usage, trace_out = await _run_drafting_stage(
                websocket, investigation_id, content, cancel_event, trace
            )
            trace_output = trace_out

        else:
            await _send(websocket, {
                "type": "text_delta",
                "content": f"Stage '{current_stage}' is not yet implemented.",
            })

        await _send(websocket, {"type": "turn_complete"})

    except Exception:
        logger.exception("Error handling message for %s", investigation_id)
        await _send(websocket, {"type": "error", "content": "Error processing message"})
        await _send(websocket, {"type": "turn_complete"})
    finally:
        trace.end(output=trace_output or assistant_text)


async def _handle_gate_decision(
    websocket: WebSocket,
    investigation_id: uuid.UUID,
    msg: dict,
) -> dict | None:
    """Process a gate decision from the journalist. Returns result dict with next_stage."""
    action = msg.get("action", "")
    feedback = msg.get("feedback")
    metadata = msg.get("metadata")  # Optional metadata (e.g., validated_findings, selected_angle_id)

    if action not in ("approve", "revise", "reject"):
        await _send(websocket, {"type": "error", "content": f"Invalid gate action: {action}"})
        return None

    try:
        async with app_session_factory() as db:
            state = await get_state(db, investigation_id)
            current_stage = state["investigation"]["current_stage"]

            result = await process_gate(
                db, investigation_id, current_stage, action, feedback, rationale=metadata
            )

        trace = TraceContext(str(investigation_id), current_stage, f"gate: {action}")
        trace.log_gate_decision(action, feedback)
        trace.end()

        await _send(websocket, {
            "type": "gate_result",
            "action": action,
            "next_stage": result.get("next_stage"),
        })

        # Only send stage_started if NOT auto-starting (auto-start handles this)
        if result.get("next_stage") and result["next_stage"] != "drafting":
            await _send(websocket, {
                "type": "stage_started",
                "stage": result["next_stage"],
            })

        return result

    except Exception:
        logger.exception("Gate decision failed for %s", investigation_id)
        await _send(websocket, {"type": "error", "content": "Failed to process gate decision"})
        return None


async def _load_conversation(
    db, investigation_id: uuid.UUID, stage: str
) -> list[dict]:
    """Load conversation history from DB as Anthropic message format."""
    result = await db.execute(
        select(Message)
        .where(
            Message.investigation_id == investigation_id,
            Message.stage == stage,
        )
        .order_by(Message.created_at)
    )
    rows = result.scalars().all()

    messages = []
    for row in rows:
        if row.role in ("user", "assistant"):
            messages.append({
                "role": row.role,
                "content": row.content,
            })

    return messages


async def _get_latest_feedback(
    investigation_id: uuid.UUID, stage: str
) -> str | None:
    """Get the latest revision feedback for a stage."""
    async with app_session_factory() as db:
        from api.models import GateLog
        result = await db.execute(
            select(GateLog)
            .where(
                GateLog.investigation_id == investigation_id,
                GateLog.stage == stage,
                GateLog.action.in_(["revise", "reject"]),
            )
            .order_by(GateLog.created_at.desc())
            .limit(1)
        )
        gate = result.scalar()
        return gate.feedback if gate else None


async def _hydrate_assets(
    parla_db,
    ini_ids: list[str],
) -> tuple[list[dict], list[dict]]:
    """Hydrate full initiative + vote data from Parla DB for curated ini_ids."""
    if not ini_ids:
        return [], []

    placeholders = ", ".join(f":id_{i}" for i in range(len(ini_ids)))
    bind = {f"id_{i}": v for i, v in enumerate(ini_ids)}

    # Fetch initiatives with latest vote
    ini_sql = f"""
        SELECT i.id, i.ini_id, i.number, i.title, i.type_description, i.author_name,
               i.current_status, i.legislature,
               COALESCE(i.llm_summary, i.summary) AS summary,
               v.resultado AS vote_result, v.favor, v.contra, v.abstencao, v.vote_date
        FROM iniciativas i
        LEFT JOIN LATERAL (
            SELECT resultado, favor, contra, abstencao, vote_date
            FROM votes WHERE iniciativa_id = i.id
            ORDER BY vote_date DESC NULLS LAST LIMIT 1
        ) v ON true
        WHERE i.ini_id IN ({placeholders})
        ORDER BY i.id DESC
    """
    result = await parla_db.execute(text(ini_sql), bind)
    ini_rows = result.mappings().all()

    initiatives = [
        {
            "id": r["id"],
            "ini_id": r["ini_id"],
            "number": r["number"],
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
        for r in ini_rows
    ]

    # Fetch all votes for these initiatives
    vote_sql = f"""
        SELECT v.id, v.iniciativa_id, i.ini_id, i.title, i.author_name,
               v.phase_name, v.vote_date, v.resultado, v.unanime,
               v.favor, v.contra, v.abstencao, v.detalhe
        FROM votes v
        JOIN iniciativas i ON i.id = v.iniciativa_id
        WHERE i.ini_id IN ({placeholders})
        ORDER BY v.vote_date DESC NULLS LAST
    """
    result = await parla_db.execute(text(vote_sql), bind)
    vote_rows = result.mappings().all()

    votes = [
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
        for r in vote_rows
    ]

    logger.info("Hydrated assets: %d initiatives, %d votes", len(initiatives), len(votes))
    return initiatives, votes


async def _upsert_research_assets(
    db,
    investigation_id: uuid.UUID,
    initiatives: list[dict],
    votes: list[dict],
) -> None:
    """Replace research assets for an investigation (full replacement, no merge)."""
    result = await db.execute(
        select(ResearchAssets).where(ResearchAssets.investigation_id == investigation_id)
    )
    existing = result.scalar()

    if existing:
        existing.initiatives = initiatives
        existing.votes = votes
    else:
        db.add(ResearchAssets(
            investigation_id=investigation_id,
            initiatives=initiatives,
            votes=votes,
        ))

    await db.commit()


async def _send(websocket: WebSocket, data: dict) -> None:
    """Send a JSON message to the WebSocket client."""
    try:
        await websocket.send_json(data)
    except Exception:
        logger.debug("Failed to send WS message: %s", data.get("type"))
