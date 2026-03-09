"""WebSocket endpoint for real-time agent chat.

Endpoint: GET /ws/chat/{investigation_id}

Protocol:
  Client -> Server: message, gate_decision, cancel
  Server -> Client: connected, text_delta, thinking, tool_call, tool_result,
                    usage, stage_output, gate_ready, gate_result, stage_started,
                    turn_complete, error
"""

import asyncio
import json
import logging
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from api.db import app_session_factory, parla_session_factory
from api.executor import run_agent, run_extraction
from api.models import Message
from api.orchestrator import (
    get_state,
    log_api_call,
    process_gate,
    save_output,
    start_stage,
)
from api.research import (
    DOSSIER_SCHEMA,
    build_research_prompt,
    get_extraction_prompt,
)
from api.tools import build_tool_registry
from api.tracing import TraceContext

from sqlalchemy import select

logger = logging.getLogger(__name__)

ws_router = APIRouter()

# Default model if client doesn't specify
DEFAULT_MODEL = "claude-sonnet-4-6"


@ws_router.websocket("/ws/chat/{investigation_id}")
async def websocket_chat(websocket: WebSocket, investigation_id: uuid.UUID):
    await websocket.accept()

    # Agent task handle (for cancellation)
    agent_task: asyncio.Task | None = None
    cancel_event = asyncio.Event()

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
                cancel_event.set()
                if agent_task and not agent_task.done():
                    agent_task.cancel()
                await _send(websocket, {"type": "turn_complete"})
                cancel_event.clear()

            elif msg_type == "gate_decision":
                # Cancel any running agent before processing gate
                if agent_task and not agent_task.done():
                    cancel_event.set()
                    agent_task.cancel()
                    try:
                        await agent_task
                    except asyncio.CancelledError:
                        pass
                    cancel_event.clear()
                await _handle_gate_decision(
                    websocket, investigation_id, msg
                )

            else:
                # Default: treat as user message
                content = msg.get("content", "")
                model = msg.get("model", DEFAULT_MODEL)

                if not content.strip():
                    continue

                # Reset cancel event for new turn
                cancel_event.clear()

                # Run agent in a task so we can cancel it
                agent_task = asyncio.create_task(
                    _handle_message(
                        websocket, investigation_id, content, model, cancel_event
                    )
                )
                try:
                    await agent_task
                except asyncio.CancelledError:
                    logger.info("Agent task cancelled for %s", investigation_id)

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: %s", investigation_id)
        cancel_event.set()
        if agent_task and not agent_task.done():
            agent_task.cancel()
    except Exception:
        logger.exception("WebSocket error for %s", investigation_id)
        cancel_event.set()
        try:
            await _send(websocket, {"type": "error", "content": "Internal server error"})
        except Exception:
            pass


async def _handle_message(
    websocket: WebSocket,
    investigation_id: uuid.UUID,
    content: str,
    model: str,
    cancel_event: asyncio.Event,
) -> None:
    """Handle a user message: persist, build prompt, run agent, stream results."""
    trace = TraceContext(str(investigation_id), "research", content)
    assistant_text = ""

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

            # Check if this is the first message (auto-start)
            is_kickoff = len(messages) <= 1

        # Build prompt and tools based on current stage
        if current_stage == "research":
            # Check for revision feedback
            feedback = await _get_latest_feedback(investigation_id, current_stage)
            system_prompt = await build_research_prompt(
                state["investigation"]["topic"], feedback
            )

            async with app_session_factory() as app_db:
                tool_defs, tool_handlers = build_tool_registry(
                    parla_session_factory, app_db, investigation_id, current_stage
                )

                # If kickoff, use the kickoff message format
                if is_kickoff and messages:
                    pass  # User message already in history

                # Run agent
                gate_requested = False
                total_usage = {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_create_tokens": 0,
                    "cost_usd": 0.0,
                }

                async for event in run_agent(
                    model=model,
                    system_prompt=system_prompt,
                    messages=messages,
                    tools=tool_defs,
                    tool_handlers=tool_handlers,
                    cancel_event=cancel_event,
                ):
                    event_type = event.get("type")

                    if event_type == "text_delta":
                        assistant_text += event.get("content", "")
                        await _send(websocket, event)

                    elif event_type == "thinking":
                        await _send(websocket, event)

                    elif event_type == "tool_call":
                        await _send(websocket, {
                            "type": "tool_call",
                            "tool": event.get("tool"),
                            "summary": event.get("summary"),
                        })

                    elif event_type == "tool_result":
                        await _send(websocket, {
                            "type": "tool_result",
                            "tool": event.get("tool"),
                            "summary": event.get("summary"),
                            "row_count": event.get("row_count"),
                        })
                        if event.get("gate_requested"):
                            gate_requested = True

                    elif event_type == "usage":
                        total_usage["input_tokens"] += event.get("input_tokens", 0)
                        total_usage["output_tokens"] += event.get("output_tokens", 0)
                        total_usage["cache_read_tokens"] += event.get("cache_read_tokens", 0)
                        total_usage["cache_create_tokens"] += event.get("cache_create_tokens", 0)
                        total_usage["cost_usd"] += event.get("cost_usd", 0.0)
                        await _send(websocket, {
                            "type": "usage",
                            "input_tokens": total_usage["input_tokens"],
                            "output_tokens": total_usage["output_tokens"],
                            "cache_read_tokens": total_usage["cache_read_tokens"],
                            "cache_create_tokens": total_usage["cache_create_tokens"],
                            "cost_usd": total_usage["cost_usd"],
                            "iteration": event.get("iteration", 0),
                        })

                        # Log to DB
                        trace.log_generation(
                            model=event.get("model", model),
                            input_messages=messages[-2:],
                            output=assistant_text[-500:],
                            usage=event,
                            iteration=event.get("iteration", 0),
                        )

                    elif event_type == "error":
                        await _send(websocket, event)

                    elif event_type == "agent_done":
                        pass  # Handled below

                # Persist assistant message
                if assistant_text.strip():
                    async with app_session_factory() as app_db:
                        assistant_msg = Message(
                            investigation_id=investigation_id,
                            stage=current_stage,
                            role="assistant",
                            content=assistant_text,
                            metadata_={"model": model, "usage": total_usage},
                        )
                        app_db.add(assistant_msg)
                        await app_db.commit()

                # Log API call
                async with app_session_factory() as app_db:
                    await log_api_call(
                        app_db,
                        investigation_id,
                        current_stage,
                        model,
                        **{k: v for k, v in total_usage.items() if k != "cost_usd"},
                    )

                # If gate review was requested, run extraction
                if gate_requested:
                    await _run_gate_extraction(
                        websocket, investigation_id, current_stage, model, messages
                    )

        else:
            # Placeholder for other stages — just echo back
            await _send(websocket, {
                "type": "text_delta",
                "content": f"Stage '{current_stage}' agent is not yet implemented. "
                           f"Only the 'research' stage is available in Phase 1.",
            })

        await _send(websocket, {"type": "turn_complete"})

    except Exception:
        logger.exception("Error handling message for %s", investigation_id)
        await _send(websocket, {"type": "error", "content": "Error processing message"})
        await _send(websocket, {"type": "turn_complete"})
    finally:
        trace.end(output=assistant_text)


async def _run_gate_extraction(
    websocket: WebSocket,
    investigation_id: uuid.UUID,
    stage: str,
    model: str,
    messages: list[dict],
) -> None:
    """Run structured extraction after request_gate_review and send results."""
    try:
        parsed_output, extraction_usage = await run_extraction(
            model=model,
            messages=messages,
            schema=DOSSIER_SCHEMA,
            extraction_prompt=get_extraction_prompt(),
        )

        # Save output to DB
        async with app_session_factory() as app_db:
            await save_output(app_db, investigation_id, stage, parsed_output)

        # Send stage output to client
        await _send(websocket, {
            "type": "stage_output",
            "stage": stage,
            "output": parsed_output,
        })

        # Send gate_ready
        summary = parsed_output.get("executive_summary", "")[:200]
        await _send(websocket, {
            "type": "gate_ready",
            "stage": stage,
            "summary": summary,
        })

        # Send extraction usage
        extraction_cost = extraction_usage.get("cost_usd", 0)
        if not extraction_cost:
            from api.costs import calculate_cost
            extraction_cost = calculate_cost(
                model,
                extraction_usage.get("input_tokens", 0),
                extraction_usage.get("output_tokens", 0),
                extraction_usage.get("cache_read_tokens", 0),
                extraction_usage.get("cache_create_tokens", 0),
            )
        await _send(websocket, {
            "type": "usage",
            "input_tokens": extraction_usage.get("input_tokens", 0),
            "output_tokens": extraction_usage.get("output_tokens", 0),
            "cache_read_tokens": extraction_usage.get("cache_read_tokens", 0),
            "cache_create_tokens": extraction_usage.get("cache_create_tokens", 0),
            "cost_usd": extraction_cost,
            "iteration": 0,
        })

    except Exception:
        logger.exception("Extraction failed for %s/%s", investigation_id, stage)
        await _send(websocket, {
            "type": "error",
            "content": "Failed to extract structured dossier. The agent's research is preserved — try again.",
        })


async def _handle_gate_decision(
    websocket: WebSocket,
    investigation_id: uuid.UUID,
    msg: dict,
) -> None:
    """Process a gate decision from the journalist."""
    action = msg.get("action", "")
    feedback = msg.get("feedback")

    if action not in ("approve", "revise", "reject"):
        await _send(websocket, {"type": "error", "content": f"Invalid gate action: {action}"})
        return

    try:
        async with app_session_factory() as db:
            state = await get_state(db, investigation_id)
            current_stage = state["investigation"]["current_stage"]

            result = await process_gate(
                db, investigation_id, current_stage, action, feedback
            )

        trace = TraceContext(str(investigation_id), current_stage, f"gate: {action}")
        trace.log_gate_decision(action, feedback)
        trace.end()

        await _send(websocket, {
            "type": "gate_result",
            "action": action,
            "next_stage": result.get("next_stage"),
        })

        if result.get("next_stage"):
            await _send(websocket, {
                "type": "stage_started",
                "stage": result["next_stage"],
            })

    except Exception:
        logger.exception("Gate decision failed for %s", investigation_id)
        await _send(websocket, {"type": "error", "content": "Failed to process gate decision"})


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


async def _send(websocket: WebSocket, data: dict) -> None:
    """Send a JSON message to the WebSocket client."""
    try:
        await websocket.send_json(data)
    except Exception:
        logger.debug("Failed to send WS message: %s", data.get("type"))
