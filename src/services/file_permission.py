from __future__ import annotations

import uuid

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import (
    File,
    FilePermission,
    FilePermissionLevel,
    User,
    WorkspaceMember,
    WorkspaceRole,
)
from src.models.file import FileSecurityMode
from src.schemas.file_permission import FilePermissionOut

# ---------------------------------------------------------------------------
# Role helper
# ---------------------------------------------------------------------------

_ROLE_ORDER = [
    WorkspaceRole.guest,
    WorkspaceRole.member,
    WorkspaceRole.admin,
    WorkspaceRole.owner,
]


def _role_gte(role: WorkspaceRole, min_role: WorkspaceRole) -> bool:
    return _ROLE_ORDER.index(role) >= _ROLE_ORDER.index(min_role)


async def _require_admin(
    workspace_id: uuid.UUID, user: User, db: AsyncSession
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
    if not _role_gte(member.role, WorkspaceRole.admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Requires admin role"
        )
    return member


async def _load_file(
    workspace_id: uuid.UUID, file_id: uuid.UUID, db: AsyncSession
) -> File:
    file = await db.scalar(
        select(File).where(File.id == file_id, File.workspace_id == workspace_id)
    )
    if file is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
        )
    return file


# ---------------------------------------------------------------------------
# List permissions
# ---------------------------------------------------------------------------


async def list_permissions(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    caller: User,
    db: AsyncSession,
) -> list[FilePermissionOut]:
    await _require_admin(workspace_id, caller, db)
    file = await _load_file(workspace_id, file_id, db)

    if file.security_mode != FileSecurityMode.per_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File is not in per_user security mode",
        )

    result = await db.execute(
        select(FilePermission, User)
        .join(User, User.id == FilePermission.user_id)
        .where(FilePermission.file_id == file_id)
    )
    rows = result.all()
    return [
        FilePermissionOut(
            id=fp.id,
            file_id=fp.file_id,
            user_id=fp.user_id,
            user_email=u.email,
            user_display_name=u.display_name,
            permission=fp.permission,
            granted_by=fp.granted_by,
            created_at=fp.created_at,
            updated_at=fp.updated_at,
        )
        for fp, u in rows
    ]


# ---------------------------------------------------------------------------
# Upsert (grant / change) permission for a user
# ---------------------------------------------------------------------------


async def upsert_permission(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    target_user_id: uuid.UUID,
    permission: FilePermissionLevel,
    caller: User,
    db: AsyncSession,
) -> FilePermissionOut:
    await _require_admin(workspace_id, caller, db)
    file = await _load_file(workspace_id, file_id, db)

    if file.security_mode != FileSecurityMode.per_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Switch file to per_user security mode before managing per-user permissions",
        )

    target = await db.get(User, target_user_id)
    if target is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="User not found"
        )

    existing = await db.scalar(
        select(FilePermission).where(
            FilePermission.file_id == file_id,
            FilePermission.user_id == target_user_id,
        )
    )
    if existing:
        existing.permission = permission
        existing.granted_by = caller.id
        fp = existing
    else:
        fp = FilePermission(
            file_id=file_id,
            user_id=target_user_id,
            permission=permission,
            granted_by=caller.id,
        )
        db.add(fp)

    await db.commit()
    await db.refresh(fp)

    return FilePermissionOut(
        id=fp.id,
        file_id=fp.file_id,
        user_id=fp.user_id,
        user_email=target.email,
        user_display_name=target.display_name,
        permission=fp.permission,
        granted_by=fp.granted_by,
        created_at=fp.created_at,
        updated_at=fp.updated_at,
    )


# ---------------------------------------------------------------------------
# Revoke (remove) permission entry
# ---------------------------------------------------------------------------


async def revoke_permission(
    workspace_id: uuid.UUID,
    file_id: uuid.UUID,
    target_user_id: uuid.UUID,
    caller: User,
    db: AsyncSession,
) -> None:
    await _require_admin(workspace_id, caller, db)
    await _load_file(workspace_id, file_id, db)

    fp = await db.scalar(
        select(FilePermission).where(
            FilePermission.file_id == file_id,
            FilePermission.user_id == target_user_id,
        )
    )
    if fp is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Permission entry not found"
        )

    await db.delete(fp)
    await db.commit()


# ---------------------------------------------------------------------------
# RAG helper: excluded_file_ids for a given user
# ---------------------------------------------------------------------------


async def get_excluded_file_ids(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession,
) -> list[str]:
    """
    Returns file_ids where the user has permission=none in per_user mode.
    owner/admin callers should skip this and pass [] directly.
    Query is O(restrictions), not O(files).
    """
    result = await db.execute(
        select(FilePermission.file_id)
        .join(File, File.id == FilePermission.file_id)
        .where(
            FilePermission.user_id == user_id,
            FilePermission.permission == FilePermissionLevel.none,
            File.workspace_id == workspace_id,
            File.security_mode == FileSecurityMode.per_user,
        )
    )
    return [str(row[0]) for row in result.fetchall()]
