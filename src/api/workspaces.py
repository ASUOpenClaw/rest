import uuid

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.deps import CurrentAuth
from src.schemas.workspace import (
    InviteCreateRequest,
    InviteOut,
    JoinRequest,
    MemberAddRequest,
    MemberListOut,
    MemberOut,
    MemberPatchRequest,
    WorkspaceCreateRequest,
    WorkspaceListItem,
    WorkspaceListOut,
    WorkspaceOut,
    WorkspacePatchRequest,
    WorkspaceStats,
)
from src.services import workspace as ws_svc

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


def _ws_out(ws, stats) -> WorkspaceOut:
    return WorkspaceOut(
        id=ws.id,
        name=ws.name,
        description=ws.description,
        system_prompt=ws.system_prompt,
        config=ws.config,
        created_by=ws.created_by,
        created_at=ws.created_at,
        updated_at=ws.updated_at,
        stats=stats,
    )


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------


@router.post("", response_model=WorkspaceOut, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    body: WorkspaceCreateRequest,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    ws, stats = await ws_svc.create_workspace(
        user=auth.user,
        name=body.name,
        description=body.description,
        system_prompt=body.system_prompt,
        config=body.config,
        db=db,
    )
    return _ws_out(ws, stats)


@router.get("", response_model=WorkspaceListOut)
async def list_workspaces(
    auth: CurrentAuth,
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=20, ge=1, le=100),
    search: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    items, total = await ws_svc.list_workspaces(
        user=auth.user, page=page, per_page=per_page, search=search, db=db
    )
    return WorkspaceListOut(
        items=[
            WorkspaceListItem(
                id=ws.id,
                name=ws.name,
                description=ws.description,
                role=role,
                stats=stats,
                created_at=ws.created_at,
                updated_at=ws.updated_at,
            )
            for ws, role, stats in items
        ],
        total=total,
        page=page,
        per_page=per_page,
    )


@router.get("/{workspace_id}", response_model=WorkspaceOut)
async def get_workspace(
    workspace_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    ws, stats = await ws_svc.get_workspace(
        workspace_id=workspace_id, user=auth.user, db=db
    )
    return _ws_out(ws, stats)


@router.patch("/{workspace_id}", response_model=WorkspaceOut)
async def update_workspace(
    workspace_id: uuid.UUID,
    body: WorkspacePatchRequest,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    ws, stats = await ws_svc.update_workspace(
        workspace_id=workspace_id,
        user=auth.user,
        name=body.name,
        description=body.description,
        system_prompt=body.system_prompt,
        config=body.config,
        db=db,
    )
    return _ws_out(ws, stats)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    await ws_svc.delete_workspace(workspace_id=workspace_id, user=auth.user, db=db)


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


@router.get("/{workspace_id}/members", response_model=MemberListOut)
async def list_members(
    workspace_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    members = await ws_svc.list_members(
        workspace_id=workspace_id, user=auth.user, db=db
    )
    return MemberListOut(items=[MemberOut(**m) for m in members], total=len(members))


@router.post(
    "/{workspace_id}/members",
    response_model=MemberOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_member(
    workspace_id: uuid.UUID,
    body: MemberAddRequest,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    data = await ws_svc.add_member(
        workspace_id=workspace_id,
        caller=auth.user,
        email=body.email,
        role=body.role,
        db=db,
    )
    return MemberOut(**data)


@router.patch("/{workspace_id}/members/{user_id}", response_model=MemberOut)
async def update_member(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    body: MemberPatchRequest,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    data = await ws_svc.update_member_role(
        workspace_id=workspace_id,
        target_user_id=user_id,
        caller=auth.user,
        new_role=body.role,
        db=db,
    )
    return MemberOut(**data)


@router.delete(
    "/{workspace_id}/members/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    await ws_svc.remove_member(
        workspace_id=workspace_id,
        target_user_id=user_id,
        caller=auth.user,
        db=db,
    )


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


@router.post(
    "/{workspace_id}/invites",
    response_model=InviteOut,
    status_code=status.HTTP_201_CREATED,
)
async def create_invite(
    workspace_id: uuid.UUID,
    body: InviteCreateRequest,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    invite = await ws_svc.create_invite(
        workspace_id=workspace_id,
        caller=auth.user,
        role=body.role,
        max_uses=body.max_uses,
        expires_in_hours=body.expires_in_hours,
        db=db,
    )
    return InviteOut.model_validate(invite)


@router.post("/join", response_model=WorkspaceOut)
async def join_workspace(
    body: JoinRequest,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    ws, role = await ws_svc.join_via_invite(
        invite_code=body.invite_code, user=auth.user, db=db
    )
    from src.services.workspace import _compute_stats

    stats = await _compute_stats(ws.id, db)
    return _ws_out(ws, stats)
