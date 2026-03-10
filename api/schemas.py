"""Pydantic schemas for REST API."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class InvestigationCreate(BaseModel):
    topic: str = Field(..., min_length=1, max_length=500)


class InvestigationResponse(BaseModel):
    id: uuid.UUID
    topic: str
    slug: str
    status: str
    current_stage: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class StageResponse(BaseModel):
    id: uuid.UUID
    investigation_id: uuid.UUID
    stage: str
    status: str
    revision_count: int
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class MessageResponse(BaseModel):
    id: uuid.UUID
    investigation_id: uuid.UUID
    stage: str
    role: str
    content: str
    metadata_: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class StageOutputResponse(BaseModel):
    id: uuid.UUID
    investigation_id: uuid.UUID
    stage: str
    version: int
    output_data: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class ResearchAssetsResponse(BaseModel):
    initiatives: list[dict]
    votes: list[dict]
