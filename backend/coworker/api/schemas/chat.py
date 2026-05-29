"""Pydantic v2 boundary models for the /api/v1/conversations routes."""
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ConversationSummary(BaseModel):
    """One row in GET /api/v1/conversations and the create response."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str | None
    created_at: datetime
    updated_at: datetime


class ConversationListResponse(BaseModel):
    conversations: list[ConversationSummary]


class ConversationCreateRequest(BaseModel):
    title: str | None = None


class ChatMessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    role: str
    content: str
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    error: str | None
    created_at: datetime


class MessageHistoryResponse(BaseModel):
    messages: list[ChatMessageOut]


class MessageSendRequest(BaseModel):
    content: str = Field(min_length=1, max_length=50000)
