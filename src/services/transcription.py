from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import AsyncSessionLocal
from src.models import File, TranscriptionTask, WorkspaceMember, WorkspaceRole
from src.models.transcription import TranscriptionStatus
from src.services import s3 as s3_svc
from src.services import speaches_client

logger = logging.getLogger(__name__)

_ROLE_ORDER = [
    WorkspaceRole.guest,
    WorkspaceRole.member,
    WorkspaceRole.admin,
    WorkspaceRole.owner,
]


def _role_gte(role: WorkspaceRole, min_role: WorkspaceRole) -> bool:
    return _ROLE_ORDER.index(role) >= _ROLE_ORDER.index(min_role)


async def enqueue(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    file_id: uuid.UUID,
    language: str | None,
    include_timestamps: bool,
    db: AsyncSession,
) -> TranscriptionTask:
    """Create task record (status=processing) and spawn background worker.
    Returns immediately so the HTTP worker is not blocked.
    """
    member = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    if member is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a workspace member")
    if not _role_gte(member.role, WorkspaceRole.member):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Requires member role")

    file = await db.scalar(
        select(File).where(File.id == file_id, File.workspace_id == workspace_id)
    )
    if file is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="File not found")

    task = TranscriptionTask(
        workspace_id=workspace_id,
        file_id=file_id,
        requested_by=user_id,
        language=language,
        include_timestamps=include_timestamps,
        status=TranscriptionStatus.processing,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    asyncio.create_task(
        _run_transcription(task.id, file.s3_key, file.original_name, file.mime_type, language, include_timestamps)
    )
    return task


async def _run_transcription(
    task_id: uuid.UUID,
    s3_key: str,
    filename: str,
    mime_type: str,
    language: str | None,
    include_timestamps: bool,
) -> None:
    """Background worker — runs outside the request lifecycle with its own DB session."""
    started_at = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        task = await db.get(TranscriptionTask, task_id)
        if task is None:
            logger.error("transcription task %s not found in background worker", task_id)
            return
        try:
            file_bytes = await s3_svc.download_bytes(s3_key)
            result = await speaches_client.transcribe(
                file_bytes=file_bytes,
                filename=filename,
                mime_type=mime_type,
                language=language,
                include_timestamps=include_timestamps,
            )
            elapsed = (datetime.now(UTC) - started_at).total_seconds()
            task.status = TranscriptionStatus.completed
            task.result = result
            task.processing_time_sec = elapsed
            task.completed_at = datetime.now(UTC)
        except Exception as exc:
            logger.error("transcription task %s failed: %s", task_id, exc)
            task.status = TranscriptionStatus.failed
            task.error = str(exc)
            task.completed_at = datetime.now(UTC)
        await db.commit()


async def transcribe(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    file_id: uuid.UUID,
    language: str | None,
    include_timestamps: bool,
    db: AsyncSession,
) -> TranscriptionTask:
    member = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not a workspace member"
        )
    if not _role_gte(member.role, WorkspaceRole.member):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Requires member role"
        )

    file = await db.scalar(
        select(File).where(File.id == file_id, File.workspace_id == workspace_id)
    )
    if file is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    # Create task record (pending → will update in same request)
    task = TranscriptionTask(
        workspace_id=workspace_id,
        file_id=file_id,
        requested_by=user_id,
        language=language,
        include_timestamps=include_timestamps,
        status=TranscriptionStatus.processing,
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    started_at = datetime.now(UTC)

    try:
        file_bytes = await s3_svc.download_bytes(file.s3_key)
        result = await speaches_client.transcribe(
            file_bytes=file_bytes,
            filename=file.original_name,
            mime_type=file.mime_type,
            language=language,
            include_timestamps=include_timestamps,
        )
    except HTTPException:
        task.status = TranscriptionStatus.failed
        task.error = "Transcription service error"
        task.completed_at = datetime.now(UTC)
        await db.commit()
        raise
    except Exception as exc:
        task.status = TranscriptionStatus.failed
        task.error = str(exc)
        task.completed_at = datetime.now(UTC)
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Transcription failed",
        )

    elapsed = (datetime.now(UTC) - started_at).total_seconds()
    task.status = TranscriptionStatus.completed
    task.result = result
    task.processing_time_sec = elapsed
    task.completed_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(task)
    return task


async def get_task(
    workspace_id: uuid.UUID,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> TranscriptionTask:
    member = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not a workspace member"
        )

    task = await db.scalar(
        select(TranscriptionTask).where(
            TranscriptionTask.id == task_id,
            TranscriptionTask.workspace_id == workspace_id,
        )
    )
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task not found"
        )

    return task
