"""REST API route handlers."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from slugify import slugify
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.db import get_app_db
from api.models import Investigation, Stage, StageOutput
from api.schemas import InvestigationCreate, InvestigationResponse, StageOutputResponse

router = APIRouter(prefix="/api")

STAGES = ["research", "analysis", "editorial", "visualization", "drafting", "qa"]


@router.post("/investigations", response_model=InvestigationResponse, status_code=201)
async def create_investigation(
    data: InvestigationCreate, db: AsyncSession = Depends(get_app_db)
):
    # Generate slug from topic
    base_slug = slugify(data.topic, max_length=100)
    slug = base_slug

    # Ensure unique slug
    counter = 1
    while True:
        result = await db.execute(select(Investigation).where(Investigation.slug == slug))
        if not result.scalar():
            break
        slug = f"{base_slug}-{counter}"
        counter += 1

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

    await db.commit()
    await db.refresh(investigation)
    return investigation


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
