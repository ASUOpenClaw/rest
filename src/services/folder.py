from __future__ import annotations

import uuid
from typing import Literal

from fastapi import HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import File, Folder, User, WorkspaceMember, WorkspaceRole
from src.schemas.folder import (
    BreadcrumbItem,
    CreatedByRef,
    FolderDetailOut,
    FolderListItem,
    FolderOut,
    FolderTreeNode,
)

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
# Path helpers via recursive CTE
# ---------------------------------------------------------------------------


async def _compute_path(folder_id: uuid.UUID, db: AsyncSession) -> str:
    """Walk up parent chain using a recursive CTE; return ' / '-joined path."""
    cte = text("""
        WITH RECURSIVE ancestors AS (
            SELECT id, name, parent_id, 0 AS depth
            FROM folders
            WHERE id = :folder_id
            UNION ALL
            SELECT f.id, f.name, f.parent_id, a.depth + 1
            FROM folders f
            JOIN ancestors a ON f.id = a.parent_id
        )
        SELECT name FROM ancestors ORDER BY depth DESC
    """)
    result = await db.execute(cte, {"folder_id": str(folder_id)})
    names = [row[0] for row in result.fetchall()]
    return " / ".join(names)


async def _compute_breadcrumbs(
    folder_id: uuid.UUID, db: AsyncSession
) -> list[BreadcrumbItem]:
    cte = text("""
        WITH RECURSIVE ancestors AS (
            SELECT id, name, parent_id, 0 AS depth
            FROM folders
            WHERE id = :folder_id
            UNION ALL
            SELECT f.id, f.name, f.parent_id, a.depth + 1
            FROM folders f
            JOIN ancestors a ON f.id = a.parent_id
        )
        SELECT id, name FROM ancestors ORDER BY depth DESC
    """)
    result = await db.execute(cte, {"folder_id": str(folder_id)})
    return [BreadcrumbItem(id=row[0], name=row[1]) for row in result.fetchall()]


async def _files_count(folder_id: uuid.UUID, db: AsyncSession) -> int:
    return await db.scalar(select(func.count()).where(File.folder_id == folder_id)) or 0


async def _children_count(folder_id: uuid.UUID, db: AsyncSession) -> int:
    return (
        await db.scalar(select(func.count()).where(Folder.parent_id == folder_id)) or 0
    )


async def _build_folder_out(folder: Folder, db: AsyncSession) -> FolderOut:
    path = await _compute_path(folder.id, db)
    fc = await _files_count(folder.id, db)
    cc = await _children_count(folder.id, db)

    created_by_ref: CreatedByRef | None = None
    if folder.created_by:
        from src.models import User as UserModel

        u = await db.get(UserModel, folder.created_by)
        if u:
            created_by_ref = CreatedByRef(id=u.id, display_name=u.display_name)

    return FolderOut(
        id=folder.id,
        name=folder.name,
        parent_id=folder.parent_id,
        path=path,
        workspace_id=folder.workspace_id,
        files_count=fc,
        children_count=cc,
        created_by=created_by_ref,
        created_at=folder.created_at,
    )


async def _build_folder_detail(folder: Folder, db: AsyncSession) -> FolderDetailOut:
    base = await _build_folder_out(folder, db)
    breadcrumbs = await _compute_breadcrumbs(folder.id, db)
    return FolderDetailOut(**base.model_dump(), breadcrumbs=breadcrumbs)


# ---------------------------------------------------------------------------
# Cycle detection
# ---------------------------------------------------------------------------


async def _is_descendant(
    folder_id: uuid.UUID, candidate_parent_id: uuid.UUID, db: AsyncSession
) -> bool:
    """Return True if candidate_parent_id is folder_id or a descendant of it."""
    cte = text("""
        WITH RECURSIVE descendants AS (
            SELECT id FROM folders WHERE id = :folder_id
            UNION ALL
            SELECT f.id FROM folders f
            JOIN descendants d ON f.parent_id = d.id
        )
        SELECT 1 FROM descendants WHERE id = :candidate_id LIMIT 1
    """)
    result = await db.execute(
        cte, {"folder_id": str(folder_id), "candidate_id": str(candidate_parent_id)}
    )
    return result.fetchone() is not None


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def create_folder(
    workspace_id: uuid.UUID,
    user: User,
    name: str,
    parent_id: uuid.UUID | None,
    db: AsyncSession,
) -> FolderOut:
    await _require_member(workspace_id, user, WorkspaceRole.member, db)

    if parent_id is not None:
        parent = await db.scalar(
            select(Folder).where(
                Folder.id == parent_id, Folder.workspace_id == workspace_id
            )
        )
        if parent is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Parent folder not found"
            )

    folder = Folder(
        workspace_id=workspace_id,
        parent_id=parent_id,
        name=name,
        created_by=user.id,
    )
    db.add(folder)
    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A folder with this name already exists in the parent",
        )
    await db.refresh(folder)
    return await _build_folder_out(folder, db)


async def list_folders(
    workspace_id: uuid.UUID,
    user: User,
    parent_id: uuid.UUID | None,
    recursive: bool,
    db: AsyncSession,
) -> dict:
    await _require_member(workspace_id, user, WorkspaceRole.guest, db)

    parent_ref = None
    if parent_id is not None:
        parent = await db.scalar(
            select(Folder).where(
                Folder.id == parent_id, Folder.workspace_id == workspace_id
            )
        )
        if parent is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Parent folder not found"
            )
        parent_path = await _compute_path(parent_id, db)
        parent_ref = {"id": parent.id, "name": parent.name, "path": parent_path}

    if recursive:
        # Return all descendants as flat list via recursive CTE
        cte = text("""
            WITH RECURSIVE descendants AS (
                SELECT id FROM folders
                WHERE workspace_id = :ws_id
                  AND (:parent_id IS NULL AND parent_id IS NULL
                       OR parent_id = :parent_id)
                UNION ALL
                SELECT f.id FROM folders f
                JOIN descendants d ON f.parent_id = d.id
            )
            SELECT id FROM descendants
        """)
        result = await db.execute(
            cte,
            {
                "ws_id": str(workspace_id),
                "parent_id": str(parent_id) if parent_id else None,
            },
        )
        folder_ids = [row[0] for row in result.fetchall()]
        folders = []
        for fid in folder_ids:
            f = await db.get(Folder, fid)
            if f:
                folders.append(f)
    else:
        result = await db.execute(
            select(Folder).where(
                Folder.workspace_id == workspace_id,
                Folder.parent_id == parent_id,
            )
        )
        folders = list(result.scalars().all())

    items: list[FolderListItem] = []
    for f in folders:
        path = await _compute_path(f.id, db)
        fc = await _files_count(f.id, db)
        cc = await _children_count(f.id, db)
        items.append(
            FolderListItem(
                id=f.id,
                name=f.name,
                parent_id=f.parent_id,
                path=path,
                files_count=fc,
                children_count=cc,
                created_at=f.created_at,
            )
        )

    return {"parent": parent_ref, "items": items, "total": len(items)}


async def get_folder_tree(
    workspace_id: uuid.UUID,
    user: User,
    db: AsyncSession,
) -> dict:
    await _require_member(workspace_id, user, WorkspaceRole.guest, db)

    result = await db.execute(select(Folder).where(Folder.workspace_id == workspace_id))
    all_folders = list(result.scalars().all())

    # files_count per folder
    fc_result = await db.execute(
        select(File.folder_id, func.count(File.id))
        .where(File.workspace_id == workspace_id, File.folder_id.isnot(None))
        .group_by(File.folder_id)
    )
    files_by_folder: dict[uuid.UUID, int] = {
        row[0]: row[1] for row in fc_result.fetchall()
    }

    total_files_result = (
        await db.scalar(select(func.count()).where(File.workspace_id == workspace_id))
        or 0
    )

    # Build tree in Python
    nodes: dict[uuid.UUID, FolderTreeNode] = {
        f.id: FolderTreeNode(
            id=f.id, name=f.name, files_count=files_by_folder.get(f.id, 0)
        )
        for f in all_folders
    }
    roots: list[FolderTreeNode] = []
    for f in all_folders:
        if f.parent_id is None:
            roots.append(nodes[f.id])
        elif f.parent_id in nodes:
            nodes[f.parent_id].children.append(nodes[f.id])

    return {
        "tree": roots,
        "total_folders": len(all_folders),
        "total_files": total_files_result,
    }


async def get_folder(
    workspace_id: uuid.UUID,
    folder_id: uuid.UUID,
    user: User,
    db: AsyncSession,
) -> FolderDetailOut:
    await _require_member(workspace_id, user, WorkspaceRole.guest, db)

    folder = await db.scalar(
        select(Folder).where(
            Folder.id == folder_id, Folder.workspace_id == workspace_id
        )
    )
    if folder is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found"
        )

    return await _build_folder_detail(folder, db)


async def update_folder(
    workspace_id: uuid.UUID,
    folder_id: uuid.UUID,
    user: User,
    name: str | None,
    new_parent_id: uuid.UUID | None,
    move_to_root: bool,
    db: AsyncSession,
) -> FolderOut:
    await _require_member(workspace_id, user, WorkspaceRole.admin, db)

    folder = await db.scalar(
        select(Folder).where(
            Folder.id == folder_id, Folder.workspace_id == workspace_id
        )
    )
    if folder is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found"
        )

    if name is not None:
        folder.name = name

    if move_to_root:
        folder.parent_id = None
    elif new_parent_id is not None:
        if new_parent_id == folder_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot move folder into itself",
            )
        if await _is_descendant(folder_id, new_parent_id, db):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot move folder into its own descendant",
            )
        parent = await db.scalar(
            select(Folder).where(
                Folder.id == new_parent_id, Folder.workspace_id == workspace_id
            )
        )
        if parent is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Target parent folder not found",
            )
        folder.parent_id = new_parent_id

    try:
        await db.commit()
    except Exception:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A folder with this name already exists in the target parent",
        )
    await db.refresh(folder)
    return await _build_folder_out(folder, db)


DeleteMode = Literal["fail", "move_to_parent", "cascade"]


async def delete_folder(
    workspace_id: uuid.UUID,
    folder_id: uuid.UUID,
    user: User,
    mode: DeleteMode,
    db: AsyncSession,
) -> None:
    await _require_member(workspace_id, user, WorkspaceRole.admin, db)

    folder = await db.scalar(
        select(Folder).where(
            Folder.id == folder_id, Folder.workspace_id == workspace_id
        )
    )
    if folder is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Folder not found"
        )

    children = (
        await db.scalar(select(func.count()).where(Folder.parent_id == folder_id)) or 0
    )
    files = (
        await db.scalar(select(func.count()).where(File.folder_id == folder_id)) or 0
    )
    is_empty = children == 0 and files == 0

    if not is_empty and mode == "fail":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Folder is not empty. Use mode=cascade or mode=move_to_parent.",
        )

    if mode == "move_to_parent":
        parent_id = folder.parent_id
        # Reparent immediate children
        result = await db.execute(select(Folder).where(Folder.parent_id == folder_id))
        for child in result.scalars().all():
            child.parent_id = parent_id
        # Move files to parent
        result = await db.execute(select(File).where(File.folder_id == folder_id))
        for f in result.scalars().all():
            f.folder_id = parent_id
        await db.flush()

    # cascade: SQLAlchemy cascade="all, delete-orphan" on Folder.children handles recursive delete
    await db.delete(folder)
    await db.commit()
