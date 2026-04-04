import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from src.models.transcription import TranscriptionStatus


class TranscriptionRequest(BaseModel):
    file_id: uuid.UUID
    language: str | None = None
    include_timestamps: bool = True


class TranscriptionTaskOut(BaseModel):
    task_id: uuid.UUID
    status: TranscriptionStatus
    file_id: uuid.UUID
    language: str | None
    include_timestamps: bool
    result: dict[str, Any] | None
    processing_time_sec: float | None
    error: str | None
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class TranscriptionSubmitOut(BaseModel):
    task_id: uuid.UUID
    status: TranscriptionStatus
    file_id: uuid.UUID
