"""Orchestrator: state machine, gate logic, snapshots, cost tracking.

All functions are plain async — no class. Reads/writes via SQLAlchemy AsyncSession.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.costs import calculate_cost
from api.models import (
    AgentLog,
    GateLog,
    Investigation,
    Stage,
    StageOutput,
)

STAGES = ["research", "analysis", "drafting"]


async def get_state(db: AsyncSession, investigation_id: uuid.UUID) -> dict:
    """Return investigation + all stages + latest output per stage."""
    inv = await db.get(Investigation, investigation_id)
    if not inv:
        raise ValueError(f"Investigation {investigation_id} not found")

    stages_result = await db.execute(
        select(Stage).where(Stage.investigation_id == investigation_id)
    )
    stages = stages_result.scalars().all()

    # Latest output per stage
    outputs = {}
    for stage_name in STAGES:
        out_result = await db.execute(
            select(StageOutput)
            .where(
                StageOutput.investigation_id == investigation_id,
                StageOutput.stage == stage_name,
            )
            .order_by(StageOutput.version.desc())
            .limit(1)
        )
        out = out_result.scalar()
        if out:
            outputs[stage_name] = {
                "version": out.version,
                "output_data": out.output_data,
                "created_at": out.created_at.isoformat(),
            }

    return {
        "investigation": {
            "id": str(inv.id),
            "topic": inv.topic,
            "slug": inv.slug,
            "status": inv.status,
            "current_stage": inv.current_stage,
            "created_at": inv.created_at.isoformat(),
            "updated_at": inv.updated_at.isoformat(),
        },
        "stages": [
            {
                "stage": s.stage,
                "status": s.status,
                "revision_count": s.revision_count,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "completed_at": s.completed_at.isoformat() if s.completed_at else None,
            }
            for s in sorted(stages, key=lambda s: STAGES.index(s.stage))
        ],
        "outputs": outputs,
    }


async def start_stage(db: AsyncSession, investigation_id: uuid.UUID, stage: str) -> None:
    """Set stage status='active', started_at=now()."""
    now = datetime.now(timezone.utc)
    await db.execute(
        update(Stage)
        .where(Stage.investigation_id == investigation_id, Stage.stage == stage)
        .values(status="active", started_at=now)
    )
    await db.execute(
        update(Investigation)
        .where(Investigation.id == investigation_id)
        .values(current_stage=stage)
    )
    await db.commit()


async def save_output(
    db: AsyncSession,
    investigation_id: uuid.UUID,
    stage: str,
    output_data: dict,
    set_gate_pending: bool = True,
) -> StageOutput:
    """Save stage output, optionally set stage status='gate_pending'.

    Args:
        set_gate_pending: If True (default), sets stage to gate_pending.
            Set to False for stages with chat-based iteration (e.g. drafting).
    """
    # Determine version (increment from latest)
    result = await db.execute(
        select(StageOutput.version)
        .where(
            StageOutput.investigation_id == investigation_id,
            StageOutput.stage == stage,
        )
        .order_by(StageOutput.version.desc())
        .limit(1)
    )
    latest_version = result.scalar() or 0

    output = StageOutput(
        investigation_id=investigation_id,
        stage=stage,
        version=latest_version + 1,
        output_data=output_data,
    )
    db.add(output)

    if set_gate_pending:
        await db.execute(
            update(Stage)
            .where(Stage.investigation_id == investigation_id, Stage.stage == stage)
            .values(status="gate_pending")
        )
    await db.commit()
    return output


async def process_gate(
    db: AsyncSession,
    investigation_id: uuid.UUID,
    stage: str,
    action: str,
    feedback: str | None = None,
    rationale: dict | None = None,
) -> dict:
    """Process gate decision: approve, revise, or reject.

    Args:
        rationale: Optional metadata dict (e.g., validated_findings, selected_angle_id)
                   stored as JSON string in GateLog.rationale column.

    Returns {"action": str, "next_stage": str | None}.
    """
    # Log gate decision (no snapshot — table unused)
    import json as json_module
    gate_entry = GateLog(
        investigation_id=investigation_id,
        stage=stage,
        action=action,
        feedback=feedback,
        rationale=json_module.dumps(rationale) if rationale else None,
    )
    db.add(gate_entry)

    result = {"action": action, "next_stage": None}
    now = datetime.now(timezone.utc)

    if action == "approve":
        # Complete current stage
        await db.execute(
            update(Stage)
            .where(Stage.investigation_id == investigation_id, Stage.stage == stage)
            .values(status="completed", completed_at=now)
        )

        # Advance to next stage
        stage_idx = STAGES.index(stage)
        if stage_idx < len(STAGES) - 1:
            next_stage = STAGES[stage_idx + 1]
            result["next_stage"] = next_stage
            await db.execute(
                update(Stage)
                .where(Stage.investigation_id == investigation_id, Stage.stage == next_stage)
                .values(status="active", started_at=now)
            )
            await db.execute(
                update(Investigation)
                .where(Investigation.id == investigation_id)
                .values(current_stage=next_stage)
            )
        else:
            # Last stage completed — mark investigation as completed
            await db.execute(
                update(Investigation)
                .where(Investigation.id == investigation_id)
                .values(status="completed")
            )

    elif action == "revise":
        # Atomic increment — avoids race condition on concurrent revise requests
        await db.execute(
            update(Stage)
            .where(Stage.investigation_id == investigation_id, Stage.stage == stage)
            .values(status="active", revision_count=Stage.revision_count + 1)
        )

    elif action == "reject":
        # Set stage back to active (keep revision_count unchanged)
        await db.execute(
            update(Stage)
            .where(Stage.investigation_id == investigation_id, Stage.stage == stage)
            .values(status="active")
        )

    await db.commit()
    return result


async def log_api_call(
    db: AsyncSession,
    investigation_id: uuid.UUID,
    stage: str,
    model: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_create_tokens: int = 0,
    thinking_tokens: int = 0,
    tool_calls_count: int = 0,
    iteration_count: int = 0,
    duration_ms: int | None = None,
) -> AgentLog:
    """Log an API call with token counts and cost."""
    cost = calculate_cost(model, input_tokens, output_tokens, cache_read_tokens, cache_create_tokens)
    entry = AgentLog(
        investigation_id=investigation_id,
        stage=stage,
        model=model,
        duration_ms=duration_ms,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_create_tokens=cache_create_tokens,
        thinking_tokens=thinking_tokens,
        cost_usd=cost,
        tool_calls_count=tool_calls_count,
        iteration_count=iteration_count,
    )
    db.add(entry)
    await db.commit()
    return entry
