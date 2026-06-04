import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.deps import CurrentAnyAuth
from src.schemas.transcription import (
    TranscriptionRequest,
    TranscriptionTaskListOut,
    TranscriptionTaskOut,
)
from src.services import transcription as transcription_svc

router = APIRouter(
    prefix="/workspaces/{workspace_id}/transcribe", tags=["transcription"]
)


def _task_out(task) -> TranscriptionTaskOut:
    return TranscriptionTaskOut(
        task_id=task.id,
        status=task.status,
        file_id=task.file_id,
        language=task.language,
        include_timestamps=task.include_timestamps,
        result=task.result,
        processing_time_sec=task.processing_time_sec,
        error=task.error,
        transcription_id=task.transcription_id,
        created_at=task.created_at,
        completed_at=task.completed_at,
    )


@router.get("", response_model=TranscriptionTaskListOut)
async def list_active_tasks(
    workspace_id: uuid.UUID,
    auth: CurrentAnyAuth,
    db: AsyncSession = Depends(get_db),
    page: int = Query(1, ge=1),
    per_page: int = Query(20, ge=1, le=100),
):
    """List pending and processing transcription tasks for the workspace."""
    tasks, total = await transcription_svc.list_active_tasks(
        workspace_id=workspace_id,
        user_id=auth.user.id,
        page=page,
        per_page=per_page,
        db=db,
    )
    return TranscriptionTaskListOut(
        items=[_task_out(t) for t in tasks],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.post("", response_model=TranscriptionTaskOut, status_code=202)
async def transcribe(
    workspace_id: uuid.UUID,
    body: TranscriptionRequest,
    auth: CurrentAnyAuth,
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
    return _task_out(task)


@router.post("/{task_id}/retry", response_model=TranscriptionTaskOut, status_code=202)
async def retry_transcription_task(
    workspace_id: uuid.UUID,
    task_id: uuid.UUID,
    auth: CurrentAnyAuth,
    db: AsyncSession = Depends(get_db),
):
    """Re-enqueue a failed transcription task. Only tasks with status='failed' can be retried."""
    task = await transcription_svc.retry_task(
        workspace_id=workspace_id,
        task_id=task_id,
        user_id=auth.user.id,
        db=db,
    )
    return _task_out(task)


@router.get("/{task_id}", response_model=TranscriptionTaskOut)
async def get_transcription_task(
    workspace_id: uuid.UUID,
    task_id: uuid.UUID,
    auth: CurrentAnyAuth,
    db: AsyncSession = Depends(get_db),
):
    """Fetch a transcription task by ID."""
    task = await transcription_svc.get_task(
        workspace_id=workspace_id,
        task_id=task_id,
        user_id=auth.user.id,
        db=db,
    )
    return _task_out(task)
