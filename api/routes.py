"""REST API route handlers."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from slugify import slugify
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import get_app_db
from api.models import Investigation, Message, Stage, StageOutput
from api.schemas import (
    InvestigationCreate,
    InvestigationResponse,
    MessageResponse,
    StageOutputResponse,
    StageResponse,
)

router = APIRouter(prefix="/api")

STAGES = ["research", "analysis", "editorial", "visualization", "drafting", "qa"]


@router.post("/investigations", response_model=InvestigationResponse, status_code=201)
async def create_investigation(
    data: InvestigationCreate, db: AsyncSession = Depends(get_app_db)
):
    # Generate slug from topic
    base_slug = slugify(data.topic, max_length=100)

    # Retry with incremented counter if slug collision (handles race condition)
    max_attempts = 10
    for attempt in range(max_attempts):
        slug = base_slug if attempt == 0 else f"{base_slug}-{attempt}"

        try:
            # Use savepoint to keep session clean on rollback
            async with db.begin_nested():
                # Create investigation
                investigation = Investigation(
                    topic=data.topic, slug=slug, status="active", current_stage="research"
                )
                db.add(investigation)
                await db.flush()

                # Create stage rows
                for i, stage in enumerate(STAGES):
                    stage_obj = Stage(
                        investigation_id=investigation.id,
                        stage=stage,
                        status="active" if i == 0 else "pending",
                    )
                    db.add(stage_obj)

            # Savepoint committed, now commit transaction
            await db.commit()
            await db.refresh(investigation)
            return investigation

        except IntegrityError as e:
            await db.rollback()
            # Check for unique constraint violation (PostgreSQL SQLSTATE 23505)
            is_unique_violation = getattr(e.orig, "sqlstate", None) == "23505"
            if is_unique_violation and attempt < max_attempts - 1:
                continue  # Try next slug
            raise HTTPException(status_code=409, detail="Could not generate unique slug") from e


@router.get("/investigations", response_model=list[InvestigationResponse])
async def list_investigations(db: AsyncSession = Depends(get_app_db)):
    result = await db.execute(
        select(Investigation).order_by(Investigation.created_at.desc())
    )
    return result.scalars().all()


@router.get("/investigations/{investigation_id}", response_model=InvestigationResponse)
async def get_investigation(investigation_id: uuid.UUID, db: AsyncSession = Depends(get_app_db)):
    result = await db.execute(select(Investigation).where(Investigation.id == investigation_id))
    investigation = result.scalar()
    if not investigation:
        raise HTTPException(status_code=404, detail="Investigation not found")
    return investigation


@router.delete("/investigations/{investigation_id}", status_code=204)
async def delete_investigation(investigation_id: uuid.UUID, db: AsyncSession = Depends(get_app_db)):
    result = await db.execute(select(Investigation).where(Investigation.id == investigation_id))
    investigation = result.scalar()
    if not investigation:
        raise HTTPException(status_code=404, detail="Investigation not found")
    await db.delete(investigation)
    await db.commit()


@router.get(
    "/investigations/{investigation_id}/stages/{stage}/output",
    response_model=StageOutputResponse | None,
)
async def get_stage_output(
    investigation_id: uuid.UUID, stage: str, db: AsyncSession = Depends(get_app_db)
):
    """Get latest stage output. Returns None if no output exists yet."""
    result = await db.execute(
        select(StageOutput)
        .where(
            StageOutput.investigation_id == investigation_id, StageOutput.stage == stage
        )
        .order_by(StageOutput.version.desc())
        .limit(1)
    )
    return result.scalar()


@router.get(
    "/investigations/{investigation_id}/stages",
    response_model=list[StageResponse],
)
async def list_stages(
    investigation_id: uuid.UUID, db: AsyncSession = Depends(get_app_db)
):
    """Get all stages for an investigation."""
    result = await db.execute(
        select(Stage).where(Stage.investigation_id == investigation_id)
    )
    stages = result.scalars().all()
    return sorted(stages, key=lambda s: STAGES.index(s.stage))


@router.get(
    "/investigations/{investigation_id}/messages",
    response_model=list[MessageResponse],
)
async def list_messages(
    investigation_id: uuid.UUID,
    limit: int = 200,
    db: AsyncSession = Depends(get_app_db),
):
    """Get messages for an investigation, ordered by creation time."""
    result = await db.execute(
        select(Message)
        .where(Message.investigation_id == investigation_id)
        .order_by(Message.created_at)
        .limit(min(limit, 500))
    )
    return result.scalars().all()
