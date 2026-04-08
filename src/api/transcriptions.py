import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.deps import CurrentAuth
from src.models import File, Transcription, WorkspaceMember
from src.schemas.transcription import TranscriptionListOut, TranscriptionOut
from src.services.file import _file_out

router = APIRouter(
    prefix="/workspaces/{workspace_id}/transcriptions", tags=["transcriptions"]
)


async def _transcription_out(t: Transcription, db: AsyncSession) -> TranscriptionOut:
    audio_file = await db.get(File, t.audio_file_id)
    transcript_file = await db.get(File, t.transcript_file_id)
    return TranscriptionOut(
        id=t.id,
        workspace_id=t.workspace_id,
        created_by=t.created_by,
        task_id=t.task_id,
        language=t.language,
        audio_file=await _file_out(audio_file, db),
        transcript_file=await _file_out(transcript_file, db),
        created_at=t.created_at,
        updated_at=t.updated_at,
    )


@router.get("", response_model=TranscriptionListOut)
async def list_transcriptions(
    workspace_id: uuid.UUID,
    auth: CurrentAuth,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
):
    """List all transcriptions in the workspace, newest first.
    Each item includes the original audio file and the transcript text file.
    """
    member = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == auth.user.id,
        )
    )
    if member is None:
        from fastapi import HTTPException, status
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a workspace member")

    total = await db.scalar(
        select(func.count()).where(Transcription.workspace_id == workspace_id)
    ) or 0

    result = await db.execute(
        select(Transcription)
        .where(Transcription.workspace_id == workspace_id)
        .order_by(Transcription.created_at.desc())
        .offset((page - 1) * per_page)
        .limit(per_page)
    )
    transcriptions = result.scalars().all()

    items = [await _transcription_out(t, db) for t in transcriptions]
    return TranscriptionListOut(items=items, total=total, page=page, per_page=per_page)


@router.get("/{transcription_id}", response_model=TranscriptionOut)
async def get_transcription_record(
    workspace_id: uuid.UUID,
    transcription_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    """Get a single transcription with audio + transcript file details."""
    from fastapi import HTTPException, status

    member = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == auth.user.id,
        )
    )
    if member is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not a workspace member")

    t = await db.scalar(
        select(Transcription).where(
            Transcription.id == transcription_id,
            Transcription.workspace_id == workspace_id,
        )
    )
    if t is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transcription not found")

    return await _transcription_out(t, db)
