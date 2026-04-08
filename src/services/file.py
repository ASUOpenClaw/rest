from __future__ import annotations

import os
import uuid
from typing import Literal

from fastapi import HTTPException, UploadFile, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import File, Folder, User, WorkspaceMember, WorkspaceRole
from src.models.file import IndexingStatus
from src.schemas.file import FileOut, FolderRef, UploaderRef
from src.services import meili
from src.services import nats as nats_svc
from src.services import s3 as s3_svc

# ---------------------------------------------------------------------------
# Role helpers
# ---------------------------------------------------------------------------

_ROLE_ORDER = [
    WorkspaceRole.guest,
    WorkspaceRole.member,
    WorkspaceRole.admin,
    WorkspaceRole.owner,
]


def _role_gte(role: WorkspaceRole, min_role: WorkspaceRole) -> bool:
    return _ROLE_ORDER.index(role) >= _ROLE_ORDER.index(min_role)


async def _require_member(
    workspace_id: uuid.UUID, user: User, min_role: WorkspaceRole, db: AsyncSession
) -> WorkspaceMember:
    member = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user.id,
        )
    )
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not a workspace member"
        )
    if not _role_gte(member.role, min_role):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role"
        )
    return member


# ---------------------------------------------------------------------------
# File → schema helpers
# ---------------------------------------------------------------------------


async def _folder_ref(
    folder_id: uuid.UUID | None, db: AsyncSession
) -> FolderRef | None:
    if folder_id is None:
        return None
    folder = await db.get(Folder, folder_id)
    if folder is None:
        return None
    # Compute path via recursive CTE
    path_result = await db.execute(
        text("""
            WITH RECURSIVE ancestors AS (
                SELECT id, name, parent_id, 0 AS depth
                FROM folders WHERE id = :fid
                UNION ALL
                SELECT f.id, f.name, f.parent_id, a.depth + 1
                FROM folders f JOIN ancestors a ON f.id = a.parent_id
            )
            SELECT name FROM ancestors ORDER BY depth DESC
        """),
        {"fid": str(folder_id)},
    )
    names = [row[0] for row in path_result.fetchall()]
    return FolderRef(id=folder.id, name=folder.name, path=" / ".join(names))


async def _uploader_ref(
    uploaded_by: uuid.UUID | None, db: AsyncSession
) -> UploaderRef | None:
    if uploaded_by is None:
        return None
    from src.models import User as UserModel

    u = await db.get(UserModel, uploaded_by)
    if u is None:
        return None
    return UploaderRef(id=u.id, display_name=u.display_name)


async def _file_out(file: File, db: AsyncSession) -> FileOut:
    return FileOut(
        id=file.id,
        original_name=file.original_name,
        mime_type=file.mime_type,
        size_bytes=file.size_bytes,
        folder=await _folder_ref(file.folder_id, db),
        description=file.description,
        s3_key=file.s3_key,
        uploaded_by=await _uploader_ref(file.uploaded_by, db),
        security_mode=file.security_mode,
        indexing_status=file.indexing_status,
        indexed_chunks=file.indexed_chunks,
        file_metadata=file.file_metadata,
        created_at=file.created_at,
        updated_at=file.updated_at,
    )


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def _s3_key(workspace_id: uuid.UUID, file_id: uuid.UUID, filename: str) -> str:
    ext = os.path.splitext(filename)[1]
    return f"{workspace_id}/files/{file_id}{ext}"


async def upload_file(
    workspace_id: uuid.UUID,
    user: User,
    upload: UploadFile,
    folder_id: uuid.UUID | None,
    description: str | None,
    auto_index: bool,
    db: AsyncSession,
) -> FileOut:
    await _require_member(workspace_id, user, WorkspaceRole.member, db)

    if folder_id is not None:
        folder = await db.scalar(
            select(Folder).where(
                Folder.id == folder_id, Folder.workspace_id == workspace_id
            )
        )
        if folder is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found"
            )

    file_id = uuid.uuid4()
    filename = upload.filename or "upload"
    mime_type = upload.content_type or "application/octet-stream"
    s3_key = _s3_key(workspace_id, file_id, filename)

    # Stream to S3
    await s3_svc.upload_fileobj(upload.file, s3_key, mime_type)

    # Determine size (seek back if possible, else unknown)
    size_bytes = 0
    try:
        pos = upload.file.tell()
        upload.file.seek(0, 2)
        size_bytes = upload.file.tell()
        upload.file.seek(pos)
    except Exception:
        pass

    file = File(
        id=file_id,
        workspace_id=workspace_id,
        folder_id=folder_id,
        original_name=filename,
        mime_type=mime_type,
        size_bytes=size_bytes,
        description=description,
        s3_key=s3_key,
        uploaded_by=user.id,
        indexing_status=IndexingStatus.pending,
    )
    db.add(file)
    await db.commit()
    await db.refresh(file)

    if auto_index:
        job_id = str(uuid.uuid4())
        await nats_svc.publish_index_job(
            job_id=job_id,
            job_type="index",
            workspace_id=str(workspace_id),
            file_id=str(file_id),
            s3_key=s3_key,
            mime_type=mime_type,
            original_name=filename,
            folder_id=str(folder_id) if folder_id else None,
        )

    await meili.index_file(
        file_id=str(file_id),
        workspace_id=str(workspace_id),
        original_name=filename,
        mime_type=mime_type,
        description=description,
        folder_path=None,  # folder path resolved on update if needed
    )

    return await _file_out(file, db)


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------

SortBy = Literal["created_at", "name", "size_bytes"]
SortOrder = Literal["asc", "desc"]


async def list_files(
    workspace_id: uuid.UUID,
    user: User,
    page: int,
    per_page: int,
    folder_id: uuid.UUID | None,
    folder_id_set: bool,
    recursive: bool,
    mime_type: str | None,
    search: str | None,
    indexing_status: str | None,
    sort_by: SortBy,
    sort_order: SortOrder,
    db: AsyncSession,
) -> tuple[list[FileOut], int]:
    await _require_member(workspace_id, user, WorkspaceRole.guest, db)

    q = select(File).where(File.workspace_id == workspace_id)

    if folder_id_set:
        if folder_id is None:
            q = q.where(File.folder_id.is_(None))
        elif recursive:
            # All descendants of folder_id via CTE
            cte = text("""
                WITH RECURSIVE descendants AS (
                    SELECT id FROM folders WHERE id = :fid
                    UNION ALL
                    SELECT f.id FROM folders f
                    JOIN descendants d ON f.parent_id = d.id
                )
                SELECT id FROM descendants
            """)
            result = await db.execute(cte, {"fid": str(folder_id)})
            folder_ids = [row[0] for row in result.fetchall()]
            q = q.where(File.folder_id.in_(folder_ids))
        else:
            q = q.where(File.folder_id == folder_id)

    if mime_type:
        q = q.where(File.mime_type == mime_type)
    if search:
        q = q.where(File.original_name.ilike(f"%{search}%"))
    if indexing_status:
        q = q.where(File.indexing_status == indexing_status)

    total = await db.scalar(select(func.count()).select_from(q.subquery())) or 0

    col = {
        "created_at": File.created_at,
        "name": File.original_name,
        "size_bytes": File.size_bytes,
    }[sort_by]
    q = q.order_by(col.asc() if sort_order == "asc" else col.desc())
    q = q.offset((page - 1) * per_page).limit(per_page)

    result = await db.execute(q)
    files = list(result.scalars().all())
    items = [await _file_out(f, db) for f in files]
    return items, total


# ---------------------------------------------------------------------------
# Get / Update / Delete
# ---------------------------------------------------------------------------


async def get_file(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    user: User,
    db: AsyncSession,
) -> FileOut:
    await _require_member(workspace_id, user, WorkspaceRole.guest, db)
    file = await db.scalar(
        select(File).where(File.id == file_id, File.workspace_id == workspace_id)
    )
    if file is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )
    return await _file_out(file, db)


async def update_file(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    user: User,
    description: str | None,
    folder_id: uuid.UUID | None,
    move_to_root: bool,
    body_security_mode,
    db: AsyncSession,
) -> FileOut:
    member = await _require_member(workspace_id, user, WorkspaceRole.member, db)

    file = await db.scalar(
        select(File).where(File.id == file_id, File.workspace_id == workspace_id)
    )
    if file is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    # Only uploader or admin+ can edit
    is_admin = _role_gte(member.role, WorkspaceRole.admin)
    is_uploader = file.uploaded_by == user.id
    if not is_admin and not is_uploader:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only file uploader or admin can edit",
        )

    if description is not None:
        file.description = description

    if body_security_mode is not None:
        if not is_admin and not is_uploader:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only file uploader or admin can change security mode",
            )
        file.security_mode = body_security_mode

    if move_to_root:
        file.folder_id = None
    elif folder_id is not None:
        folder = await db.scalar(
            select(Folder).where(
                Folder.id == folder_id, Folder.workspace_id == workspace_id
            )
        )
        if folder is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found"
            )
        file.folder_id = folder_id

    await db.commit()
    await db.refresh(file)
    return await _file_out(file, db)


async def delete_file(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    user: User,
    db: AsyncSession,
) -> None:
    member = await _require_member(workspace_id, user, WorkspaceRole.member, db)

    file = await db.scalar(
        select(File).where(File.id == file_id, File.workspace_id == workspace_id)
    )
    if file is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    is_admin = _role_gte(member.role, WorkspaceRole.admin)
    is_uploader = file.uploaded_by == user.id
    if not is_admin and not is_uploader:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only file uploader or admin can delete",
        )

    s3_key = file.s3_key

    # Delete from S3 first — if this fails, the DB row remains intact and
    # the caller can retry. Inverse (DB gone, S3 orphaned) is unrecoverable.
    await s3_svc.delete_object(s3_key)

    await db.delete(file)
    await db.commit()

    # Notify indexer to remove Qdrant chunks (best-effort).
    await nats_svc.publish_index_job(
        job_id=str(uuid.uuid4()),
        job_type="delete",
        workspace_id=str(workspace_id),
        file_id=str(file_id),
    )


# ---------------------------------------------------------------------------
# Download presigned URL
# ---------------------------------------------------------------------------


async def get_download_url(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    user: User,
    db: AsyncSession,
    expires_in: int = 3600,
) -> dict:
    await _require_member(workspace_id, user, WorkspaceRole.guest, db)

    file = await db.scalar(
        select(File).where(File.id == file_id, File.workspace_id == workspace_id)
    )
    if file is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    url = await s3_svc.generate_presigned_download_url(
        file.s3_key, expires_in=expires_in
    )
    return {
        "url": url,
        "expires_in": expires_in,
        "filename": file.original_name,
        "content_type": file.mime_type,
    }


# ---------------------------------------------------------------------------
# Reindex
# ---------------------------------------------------------------------------


async def reindex_file(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    user: User,
    db: AsyncSession,
) -> dict:
    await _require_member(workspace_id, user, WorkspaceRole.admin, db)

    file = await db.scalar(
        select(File).where(File.id == file_id, File.workspace_id == workspace_id)
    )
    if file is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )

    file.indexing_status = IndexingStatus.pending
    file.indexing_error = None
    await db.commit()

    await nats_svc.publish_index_job(
        job_id=str(uuid.uuid4()),
        job_type="reindex",
        workspace_id=str(workspace_id),
        file_id=str(file_id),
        s3_key=file.s3_key,
        mime_type=file.mime_type,
        original_name=file.original_name,
        folder_id=str(file.folder_id) if file.folder_id else None,
    )

    return {
        "file_id": file_id,
        "indexing_status": IndexingStatus.pending,
        "message": "Reindexing started",
    }
