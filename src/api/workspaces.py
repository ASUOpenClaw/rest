import uuid

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.deps import CurrentAuth
from src.core.redis import get_redis
from src.schemas.workspace import (
    CronJobCreateRequest,
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
    redis: aioredis.Redis = Depends(get_redis),
):
    ws, stats = await ws_svc.create_workspace(
        user=auth.user,
        name=body.name,
        description=body.description,
        system_prompt=body.system_prompt,
        config=body.config.model_dump(exclude_none=True),
        db=db,
        redis=redis,
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
        config=(
            body.config.model_dump(exclude_none=True)
            if body.config is not None
            else None
        ),
        db=db,
    )
    return _ws_out(ws, stats)


@router.delete("/{workspace_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace(
    workspace_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    await ws_svc.delete_workspace(
        workspace_id=workspace_id, user=auth.user, db=db, redis=redis
    )


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


# ---------------------------------------------------------------------------
# Skills (per-workspace instruction files stored in S3, injected at session start)
# ---------------------------------------------------------------------------


async def _require_ws_member(
    workspace_id: uuid.UUID, auth, db: AsyncSession, min_role=None
):
    """Check workspace membership + optional min_role. Returns WorkspaceMember."""
    from fastapi import HTTPException
    from fastapi import status as http_status
    from sqlalchemy import select as sa_select

    from src.models import WorkspaceMember, WorkspaceRole

    member = await db.scalar(
        sa_select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == auth.user.id,
        )
    )
    if member is None:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN, detail="Not a workspace member"
        )

    if min_role:
        _ORDER = [
            WorkspaceRole.guest,
            WorkspaceRole.member,
            WorkspaceRole.admin,
            WorkspaceRole.owner,
        ]
        if _ORDER.index(member.role) < _ORDER.index(min_role):
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN, detail="Requires admin role"
            )
    return member


@router.get("/{workspace_id}/skills")
async def list_workspace_skills(
    workspace_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    """List skill names stored for this workspace."""
    from src.services.workspace_skills import list_skills

    await _require_ws_member(workspace_id, auth, db)
    return {"skills": await list_skills(str(workspace_id))}


@router.get("/{workspace_id}/skills/{name}")
async def get_workspace_skill(
    workspace_id: uuid.UUID,
    name: str,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    """Get the markdown content of a skill."""
    from src.services.workspace_skills import get_skill

    await _require_ws_member(workspace_id, auth, db)
    content = await get_skill(str(workspace_id), name)
    return {"name": name, "content": content}


@router.put("/{workspace_id}/skills/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def put_workspace_skill(
    workspace_id: uuid.UUID,
    name: str,
    body: dict,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """Create or replace a skill. Body: {"content": "...markdown..."}. Requires admin/owner."""
    from src.models import WorkspaceRole
    from src.services.workspace_skills import put_skill

    await _require_ws_member(workspace_id, auth, db, min_role=WorkspaceRole.admin)
    content = body.get("content", "")
    if not content:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="content is required",
        )
    await put_skill(str(workspace_id), name, content, redis)


@router.delete("/{workspace_id}/skills/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace_skill(
    workspace_id: uuid.UUID,
    name: str,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """Delete a skill. Requires admin/owner."""
    from src.models import WorkspaceRole
    from src.services.workspace_skills import delete_skill

    await _require_ws_member(workspace_id, auth, db, min_role=WorkspaceRole.admin)
    await delete_skill(str(workspace_id), name, redis)


# ---------------------------------------------------------------------------
# Plugins (external MCP servers registered per-workspace)
# ---------------------------------------------------------------------------
# Stored in workspace.config["plugins"] = [{id, name, url, transport, tool_prefix, goclaw_mcp_server_id}]


async def _get_ws_with_creds(
    workspace_id: uuid.UUID, auth, db: AsyncSession, min_role=None
):
    """Load workspace + check membership + return (ws, api_key, agent_id)."""
    from src.models import Workspace

    member = await _require_ws_member(workspace_id, auth, db, min_role=min_role)
    ws = await db.get(Workspace, workspace_id)
    if ws is None:
        from fastapi import HTTPException
        from fastapi import status as s

        raise HTTPException(
            status_code=s.HTTP_404_NOT_FOUND, detail="Workspace not found"
        )
    cfg = ws.config or {}
    api_key = cfg.get("goclaw_api_key")
    agent_id = cfg.get("goclaw_agent_id")
    if not api_key or not agent_id:
        from fastapi import HTTPException
        from fastapi import status as s

        raise HTTPException(
            status_code=s.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Workspace not provisioned in GoClaw",
        )
    return ws, api_key, agent_id


@router.get("/{workspace_id}/plugins")
async def list_workspace_plugins(
    workspace_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    """List external MCP server plugins for this workspace."""
    ws, _, _ = await _get_ws_with_creds(workspace_id, auth, db)
    return {"plugins": (ws.config or {}).get("plugins", [])}


@router.post("/{workspace_id}/plugins", status_code=status.HTTP_201_CREATED)
async def add_workspace_plugin(
    workspace_id: uuid.UUID,
    body: dict,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    """
    Register an external MCP server as a plugin. Requires admin/owner.
    Body: {name, url, transport="streamable-http", tool_prefix?}
    """
    import uuid as uuid_mod

    from src.models import WorkspaceRole
    from src.services import goclaw_client

    ws, api_key, agent_id = await _get_ws_with_creds(
        workspace_id, auth, db, min_role=WorkspaceRole.admin
    )

    name = body.get("name", "").strip()
    url = body.get("url", "").strip()
    if not name or not url:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="name and url are required",
        )

    transport = body.get("transport", "streamable-http")
    tool_prefix = body.get("tool_prefix") or None

    mcp_server_id = await goclaw_client.register_plugin(
        api_key, agent_id, name, url, transport, tool_prefix
    )

    plugin = {
        "id": str(uuid_mod.uuid4()),
        "name": name,
        "url": url,
        "transport": transport,
        "tool_prefix": tool_prefix,
        "goclaw_mcp_server_id": mcp_server_id,
    }
    config = dict(ws.config or {})
    plugins = list(config.get("plugins", []))
    plugins.append(plugin)
    config["plugins"] = plugins
    ws.config = config
    await db.commit()
    return plugin


@router.delete(
    "/{workspace_id}/plugins/{plugin_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_workspace_plugin(
    workspace_id: uuid.UUID,
    plugin_id: str,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    """Unregister an external MCP server plugin. Requires admin/owner."""
    from src.models import WorkspaceRole
    from src.services import goclaw_client

    ws, api_key, agent_id = await _get_ws_with_creds(
        workspace_id, auth, db, min_role=WorkspaceRole.admin
    )

    config = dict(ws.config or {})
    plugins = list(config.get("plugins", []))
    plugin = next((p for p in plugins if p["id"] == plugin_id), None)
    if plugin is None:
        from fastapi import HTTPException

        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Plugin not found"
        )

    await goclaw_client.unregister_plugin(
        api_key, agent_id, plugin["goclaw_mcp_server_id"]
    )
    config["plugins"] = [p for p in plugins if p["id"] != plugin_id]
    ws.config = config
    await db.commit()


# ---------------------------------------------------------------------------
# Cron job management
# ---------------------------------------------------------------------------


def _parse_schedule(schedule: str) -> dict:
    """Convert a human-friendly schedule string to a GoClaw schedule object."""
    import re

    s = schedule.strip()
    m = re.fullmatch(r"every\s+(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)", s, re.IGNORECASE)
    if m:
        val, unit = float(m.group(1)), m.group(2).lower()
        factors = {"ms": 1, "s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
        return {"kind": "every", "everyMs": int(val * factors[unit])}
    if re.fullmatch(r"\d{13,}", s):
        return {"kind": "at", "atMs": int(s)}
    return {"kind": "cron", "expr": s}


@router.get("/{workspace_id}/sessions")
async def list_workspace_sessions(
    workspace_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    """List all GoClaw sessions for this workspace. Requires admin/owner."""
    from src.models import WorkspaceRole
    from src.services import goclaw_rpc

    ws, api_key, _ = await _get_ws_with_creds(
        workspace_id, auth, db, min_role=WorkspaceRole.admin
    )
    agent_key = (ws.config or {}).get("goclaw_agent_key", "")
    sessions = await goclaw_rpc.get_pool().list_sessions(api_key, agent_key)
    return {"sessions": sessions}


@router.get("/{workspace_id}/cron")
async def list_workspace_cron_jobs(
    workspace_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    """List cron jobs for this workspace's GoClaw agent."""
    from src.services import goclaw_rpc

    ws, api_key, _ = await _get_ws_with_creds(workspace_id, auth, db)
    agent_key = (ws.config or {}).get("goclaw_agent_key", "")
    jobs = await goclaw_rpc.get_pool().list_cron_jobs(api_key, agent_key)
    return {"jobs": jobs}


@router.post("/{workspace_id}/cron", status_code=status.HTTP_201_CREATED)
async def create_workspace_cron_job(
    workspace_id: uuid.UUID,
    body: CronJobCreateRequest,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    """Create a scheduled cron job for this workspace's agent. Requires admin/owner."""
    from src.models import WorkspaceRole
    from src.services import goclaw_rpc

    ws, api_key, _ = await _get_ws_with_creds(
        workspace_id, auth, db, min_role=WorkspaceRole.admin
    )
    agent_key = (ws.config or {}).get("goclaw_agent_key", "")
    return await goclaw_rpc.get_pool().create_cron_job(
        api_key, agent_key, body.name, _parse_schedule(body.schedule), body.message
    )


@router.delete("/{workspace_id}/cron/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workspace_cron_job(
    workspace_id: uuid.UUID,
    job_id: str,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    """Delete a cron job by ID. Requires admin/owner."""
    from src.models import WorkspaceRole
    from src.services import goclaw_rpc

    ws, api_key, _ = await _get_ws_with_creds(
        workspace_id, auth, db, min_role=WorkspaceRole.admin
    )
    agent_key = (ws.config or {}).get("goclaw_agent_key", "")
    await goclaw_rpc.get_pool().delete_cron_job(api_key, agent_key, job_id)


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
