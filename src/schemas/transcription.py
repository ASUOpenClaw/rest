import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from src.models.transcription import TranscriptionStatus
from src.schemas.file import FileOut


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
    transcription_id: uuid.UUID | None
    created_at: datetime
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class TranscriptionOut(BaseModel):
    """Finished transcription — both files fully resolved."""

    id: uuid.UUID
    workspace_id: uuid.UUID
    created_by: uuid.UUID | None
    task_id: uuid.UUID
    language: str | None
    audio_file: FileOut
    transcript_file: FileOut
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class TranscriptionListOut(BaseModel):
    items: list[TranscriptionOut]
    total: int
    page: int
    per_page: int
