"""Pydantic v2 boundary models for the /api/v1/specialists routes."""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class SpecialistSummary(BaseModel):
    """One row in GET /api/v1/specialists."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    display_name: str
    description: str
    is_enabled: bool
    model: str
    extended_thinking: bool
    active_version_id: UUID | None
    updated_at: datetime


class SpecialistListResponse(BaseModel):
    specialists: list[SpecialistSummary]


class SpecialistPromptResponse(BaseModel):
    id: UUID
    name: str
    display_name: str
    prompt_text: str
    version_number: int
    updated_at: datetime


class SpecialistPromptUpdate(BaseModel):
    prompt_text: str = Field(min_length=100, max_length=200000)
    change_summary: str = Field(min_length=10, max_length=500)
