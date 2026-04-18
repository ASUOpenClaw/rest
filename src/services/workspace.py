from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from fastapi import HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models import (
    File,
    User,
    Workspace,
    WorkspaceInvite,
    WorkspaceMember,
    WorkspaceRole,
)
from src.schemas.workspace import WorkspaceStats
from src.services import goclaw_client, meili

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Role helpers (mirrors deps.py, kept local to avoid circular imports)
# ---------------------------------------------------------------------------

_ROLE_ORDER = [
    WorkspaceRole.guest,
    WorkspaceRole.member,
    WorkspaceRole.admin,
    WorkspaceRole.owner,
]


def _role_gte(role: WorkspaceRole, min_role: WorkspaceRole) -> bool:
    return _ROLE_ORDER.index(role) >= _ROLE_ORDER.index(min_role)


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


async def _compute_stats(workspace_id: uuid.UUID, db: AsyncSession) -> WorkspaceStats:
    members_count = (
        await db.scalar(
            select(func.count()).where(WorkspaceMember.workspace_id == workspace_id)
        )
        or 0
    )
    files_agg = await db.execute(
        select(
            func.count(File.id), func.coalesce(func.sum(File.indexed_chunks), 0)
        ).where(File.workspace_id == workspace_id)
    )
    files_count, indexed_chunks = files_agg.one()
    return WorkspaceStats(
        members_count=members_count,
        files_count=files_count or 0,
        indexed_chunks=int(indexed_chunks or 0),
    )


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------


async def create_workspace(
    user: User,
    name: str,
    description: str | None,
    system_prompt: str | None,
    config: dict,
    db: AsyncSession,
    redis: aioredis.Redis | None = None,
) -> tuple[Workspace, WorkspaceStats]:
    ws = Workspace(
        name=name,
        description=description,
        system_prompt=system_prompt,
        config=config,
        created_by=user.id,
    )
    db.add(ws)
    await db.flush()  # get ws.id

    member = WorkspaceMember(
        workspace_id=ws.id,
        user_id=user.id,
        role=WorkspaceRole.owner,
    )
    db.add(member)
    await db.commit()
    await db.refresh(ws)

    # Provision GoClaw tenant + API key + agent for this workspace.
    if settings.goclaw_gateway_url and settings.goclaw_gateway_token:
        try:
            goclaw = await goclaw_client.provision_workspace(str(ws.id), ws.name)
            ws.config = {**(ws.config or {}), **goclaw}
            await db.commit()
            await db.refresh(ws)
            if redis is not None:
                await redis.setex(
                    f"ws_creds:{ws.id}",
                    3600,
                    json.dumps(
                        {
                            "api_key": goclaw["goclaw_api_key"],
                            "agent_id": goclaw["goclaw_agent_id"],
                        }
                    ),
                )
        except Exception as exc:
            logger.error(
                "GoClaw provisioning failed for workspace %s: %s: %s",
                ws.id,
                type(exc).__name__,
                exc,
            )
            # Non-fatal: workspace is created, GoClaw can be provisioned manually.

    await meili.index_workspace(str(ws.id), ws.name, ws.description)
    stats = await _compute_stats(ws.id, db)
    return ws, stats


async def list_workspaces(
    user: User,
    page: int,
    per_page: int,
    search: str | None,
    db: AsyncSession,
) -> tuple[list[tuple[Workspace, WorkspaceRole, WorkspaceStats]], int]:
    q = (
        select(Workspace, WorkspaceMember.role)
        .join(WorkspaceMember, WorkspaceMember.workspace_id == Workspace.id)
        .where(WorkspaceMember.user_id == user.id)
    )
    if search:
        q = q.where(Workspace.name.ilike(f"%{search}%"))

    total = await db.scalar(select(func.count()).select_from(q.subquery())) or 0

    q = q.offset((page - 1) * per_page).limit(per_page)
    result = await db.execute(q)
    rows = result.all()

    items = []
    for ws, role in rows:
        stats = await _compute_stats(ws.id, db)
        items.append((ws, role, stats))
    return items, total


async def get_workspace(
    workspace_id: uuid.UUID,
    user: User,
    db: AsyncSession,
) -> tuple[Workspace, WorkspaceStats]:
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

    ws = await db.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found"
        )

    stats = await _compute_stats(ws.id, db)
    return ws, stats


async def update_workspace(
    workspace_id: uuid.UUID,
    user: User,
    name: str | None,
    description: str | None,
    system_prompt: str | None,
    config: dict | None,
    db: AsyncSession,
) -> tuple[Workspace, WorkspaceStats]:
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

    ws = await db.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found"
        )

    if name is not None:
        ws.name = name
    if description is not None:
        ws.description = description
    if system_prompt is not None:
        ws.system_prompt = system_prompt
    if config is not None:
        ws.config = config

    await db.commit()
    await db.refresh(ws)
    await meili.index_workspace(str(ws.id), ws.name, ws.description)
    stats = await _compute_stats(ws.id, db)
    return ws, stats


async def delete_workspace(
    workspace_id: uuid.UUID,
    user: User,
    db: AsyncSession,
    redis: aioredis.Redis | None = None,
) -> None:
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

    ws = await db.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found"
        )

    # Best-effort: delete GoClaw tenant before removing from DB.
    tenant_id = (ws.config or {}).get("goclaw_tenant_id")
    if tenant_id and settings.goclaw_gateway_url and settings.goclaw_gateway_token:
        try:
            await goclaw_client.delete_tenant(tenant_id)
        except Exception as exc:
            logger.error(
                "GoClaw tenant deletion failed for workspace %s: %s", workspace_id, exc
            )

    await db.delete(ws)
    await db.commit()
    await meili.delete_workspace(str(workspace_id))
    if redis is not None:
        await redis.delete(f"ws_creds:{workspace_id}")


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


async def list_members(
    workspace_id: uuid.UUID,
    user: User,
    db: AsyncSession,
) -> list[dict]:
    # Verify caller is a member
    caller = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user.id,
        )
    )
    if caller is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not a workspace member"
        )

    result = await db.execute(
        select(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == workspace_id)
    )
    rows = result.all()
    return [
        {
            "user_id": m.user_id,
            "email": u.email,
            "display_name": u.display_name,
            "role": m.role,
            "joined_at": m.joined_at,
        }
        for m, u in rows
    ]


async def add_member(
    workspace_id: uuid.UUID,
    caller: User,
    email: str,
    role: WorkspaceRole,
    db: AsyncSession,
) -> dict:
    caller_member = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == caller.id,
        )
    )
    if caller_member is None or not _role_gte(caller_member.role, WorkspaceRole.admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Requires admin role"
        )

    # Cannot directly assign owner through this endpoint
    if role == WorkspaceRole.owner:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use ownership transfer to assign owner role",
        )

    # Find or create stub user
    target = await db.scalar(select(User).where(User.email == email))
    if target is None:
        # Check invite stub
        target = await db.scalar(select(User).where(User.invite_email == email))
    if target is None:
        target = User(email=email, display_name=email, invite_email=email)
        db.add(target)
        await db.flush()

    # Idempotent: update role if already a member
    existing = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == target.id,
        )
    )
    if existing:
        existing.role = role
        await db.commit()
        member_row = existing
    else:
        member_row = WorkspaceMember(
            workspace_id=workspace_id,
            user_id=target.id,
            role=role,
        )
        db.add(member_row)
        await db.commit()
        await db.refresh(member_row)

    return {
        "user_id": target.id,
        "email": target.email,
        "display_name": target.display_name,
        "role": member_row.role,
        "joined_at": member_row.joined_at,
    }


async def update_member_role(
    workspace_id: uuid.UUID,
    target_user_id: uuid.UUID,
    caller: User,
    new_role: WorkspaceRole,
    db: AsyncSession,
) -> dict:
    caller_member = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == caller.id,
        )
    )
    if caller_member is None or not _role_gte(caller_member.role, WorkspaceRole.admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Requires admin role"
        )

    # Owner transfer: only the current owner can do it
    if new_role == WorkspaceRole.owner:
        if caller_member.role != WorkspaceRole.owner:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only owner can transfer ownership",
            )
        # Demote current owner to admin
        caller_member.role = WorkspaceRole.admin

    target_member = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == target_user_id,
        )
    )
    if target_member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Member not found"
        )

    # Non-owner admins cannot modify owner or other admins of equal level
    if caller_member.role == WorkspaceRole.admin and _role_gte(
        target_member.role, WorkspaceRole.admin
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot modify owner or peer admin",
        )

    target_member.role = new_role
    await db.commit()

    target_user = await db.get(User, target_user_id)
    return {
        "user_id": target_user.id,
        "email": target_user.email,
        "display_name": target_user.display_name,
        "role": target_member.role,
        "joined_at": target_member.joined_at,
    }


async def remove_member(
    workspace_id: uuid.UUID,
    target_user_id: uuid.UUID,
    caller: User,
    db: AsyncSession,
) -> None:
    caller_member = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == caller.id,
        )
    )
    if caller_member is None or not _role_gte(caller_member.role, WorkspaceRole.admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Requires admin role"
        )

    target_member = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == target_user_id,
        )
    )
    if target_member is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Member not found"
        )

    if target_member.role == WorkspaceRole.owner:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove workspace owner",
        )

    # Prevent removing the last admin (at least one admin must remain)
    if target_member.role == WorkspaceRole.admin:
        admin_count = (
            await db.scalar(
                select(func.count()).where(
                    WorkspaceMember.workspace_id == workspace_id,
                    WorkspaceMember.role.in_(
                        [WorkspaceRole.admin, WorkspaceRole.owner]
                    ),
                )
            )
            or 0
        )
        if admin_count <= 1:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot remove last admin",
            )

    await db.delete(target_member)
    await db.commit()


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


async def create_invite(
    workspace_id: uuid.UUID,
    caller: User,
    role: WorkspaceRole,
    max_uses: int | None,
    expires_in_hours: int | None,
    db: AsyncSession,
) -> WorkspaceInvite:
    caller_member = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == caller.id,
        )
    )
    if caller_member is None or not _role_gte(caller_member.role, WorkspaceRole.admin):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Requires admin role"
        )

    if role == WorkspaceRole.owner:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot create invite for owner role",
        )

    expires_at = None
    if expires_in_hours is not None:
        expires_at = datetime.now(UTC) + timedelta(hours=expires_in_hours)

    invite = WorkspaceInvite(
        workspace_id=workspace_id,
        code=secrets.token_urlsafe(32),
        role=role,
        max_uses=max_uses,
        used_count=0,
        created_by=caller.id,
        expires_at=expires_at,
    )
    db.add(invite)
    await db.commit()
    await db.refresh(invite)
    return invite


async def join_via_invite(
    invite_code: str,
    user: User,
    db: AsyncSession,
) -> tuple[Workspace, WorkspaceRole]:
    invite = await db.scalar(
        select(WorkspaceInvite).where(WorkspaceInvite.code == invite_code)
    )
    if invite is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Invalid invite code"
        )

    if invite.expires_at and invite.expires_at < datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invite link has expired"
        )

    if invite.max_uses is not None and invite.used_count >= invite.max_uses:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invite link has reached max uses",
        )

    # Idempotent: already a member
    existing = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == invite.workspace_id,
            WorkspaceMember.user_id == user.id,
        )
    )
    if existing is None:
        db.add(
            WorkspaceMember(
                workspace_id=invite.workspace_id,
                user_id=user.id,
                role=invite.role,
            )
        )
        invite.used_count += 1

    await db.commit()

    ws = await db.get(Workspace, invite.workspace_id)
    return ws, invite.role
