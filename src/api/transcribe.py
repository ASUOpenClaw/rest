import uuid

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.deps import CurrentAuth
from src.schemas.transcription import TranscriptionRequest, TranscriptionTaskOut
from src.services import transcription as transcription_svc

router = APIRouter(
    prefix="/workspaces/{workspace_id}/transcribe", tags=["transcription"]
)


@router.post("", response_model=TranscriptionTaskOut, status_code=202)
async def transcribe(
    workspace_id: uuid.UUID,
    body: TranscriptionRequest,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    """
    Enqueue transcription of an uploaded audio/video file via Speaches (Whisper).
    Returns 202 immediately with task_id and status='processing'.
    Poll GET /{task_id} until status is 'completed' or 'failed'.
    """
    task = await transcription_svc.enqueue(
        workspace_id=workspace_id,
        user_id=auth.user.id,
        file_id=body.file_id,
        language=body.language,
        include_timestamps=body.include_timestamps,
        db=db,
    )
    return TranscriptionTaskOut(
        task_id=task.id,
        status=task.status,
        file_id=task.file_id,
        language=task.language,
        include_timestamps=task.include_timestamps,
        result=task.result,
        processing_time_sec=task.processing_time_sec,
        error=task.error,
        created_at=task.created_at,
        completed_at=task.completed_at,
    )


@router.get("/{task_id}", response_model=TranscriptionTaskOut)
async def get_transcription_task(
    workspace_id: uuid.UUID,
    task_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    """Fetch a past transcription task by ID."""
    task = await transcription_svc.get_task(
        workspace_id=workspace_id,
        task_id=task_id,
        user_id=auth.user.id,
        db=db,
    )
    return TranscriptionTaskOut(
        task_id=task.id,
        status=task.status,
        file_id=task.file_id,
        language=task.language,
        include_timestamps=task.include_timestamps,
        result=task.result,
        processing_time_sec=task.processing_time_sec,
        error=task.error,
        created_at=task.created_at,
        completed_at=task.completed_at,
    )
