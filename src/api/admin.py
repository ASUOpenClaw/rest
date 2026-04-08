import uuid

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.deps import CurrentAuth
from src.core.redis import get_redis
from src.models import Workspace, WorkspaceMember, WorkspaceRole
from src.services import goclaw_client, workspace as ws_svc

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_owner(auth):
    """Raises 403 if the caller is not a superuser (no workspace context here —
    admin endpoints are global). For now we trust any authenticated user;
    tighten with a superuser flag on User when needed."""
    pass


@router.get("/workspaces/unprovisioned")
async def list_unprovisioned(
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    """List workspaces that have no GoClaw tenant provisioned yet.
    Useful after GoClaw was down during workspace creation.
    """
    result = await db.execute(
        select(Workspace).where(
            Workspace.config["goclaw_tenant_id"].as_string() == None  # noqa: E711
        )
    )
    workspaces = result.scalars().all()
    return [{"id": str(ws.id), "name": ws.name, "created_at": ws.created_at} for ws in workspaces]


@router.post("/workspaces/{workspace_id}/reprovision", status_code=status.HTTP_200_OK)
async def reprovision_workspace(
    workspace_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """Re-run GoClaw tenant + agent provisioning for a workspace.
    Use when GoClaw was unavailable during workspace creation.
    Safe to call on already-provisioned workspaces — will skip if tenant exists.
    """
    from src.core.config import settings
    import json

    ws = await db.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found")

    if (ws.config or {}).get("goclaw_tenant_id"):
        return {"status": "already_provisioned", "workspace_id": str(workspace_id)}

    if not settings.goclaw_gateway_url or not settings.goclaw_gateway_token:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GoClaw not configured",
        )

    goclaw = await goclaw_client.provision_workspace(str(ws.id), ws.name)
    ws.config = {**(ws.config or {}), **goclaw}
    await db.commit()
    await redis.setex(
        f"ws_creds:{ws.id}",
        3600,
        json.dumps({
            "api_key": goclaw["goclaw_api_key"],
            "agent_id": goclaw["goclaw_agent_id"],
        }),
    )
    return {"status": "provisioned", "workspace_id": str(workspace_id), **goclaw}
