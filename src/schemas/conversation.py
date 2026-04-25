import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from src.models.conversation import MessageRole


class ConversationOut(BaseModel):
    id: uuid.UUID
    workspace_id: uuid.UUID
    user_id: uuid.UUID
    title: str | None
    goclaw_session_key: str | None = None
    message_count: int
    last_message_at: datetime | None
    rag_indexed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ConversationListOut(BaseModel):
    items: list[ConversationOut]
    total: int
    page: int
    per_page: int


class ConversationPatchRequest(BaseModel):
    title: str


class MessageOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    role: MessageRole
    content: str | None
    model: str | None
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageListOut(BaseModel):
    items: list[MessageOut]
    total: int
    page: int
    per_page: int
