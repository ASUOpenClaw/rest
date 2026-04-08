from __future__ import annotations

import logging
import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import File, TranscriptionTask, WorkspaceMember, WorkspaceRole
from src.models.transcription import TranscriptionStatus
from src.services import nats as nats_svc

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
    """Create TranscriptionTask and publish to NATS. Returns 202 immediately."""
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

    await nats_svc.publish_transcription_job(
        job_id=str(uuid.uuid4()),
        task_id=str(task.id),
        workspace_id=str(workspace_id),
        audio_file_id=str(file_id),
        s3_key=file.s3_key,
        filename=file.original_name,
        mime_type=file.mime_type,
        language=language,
        include_timestamps=include_timestamps,
        requested_by=str(user_id),
    )
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
