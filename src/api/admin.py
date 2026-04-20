import uuid
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.deps import CurrentAuth
from src.core.redis import get_redis
from src.models import File, Workspace, WorkspaceMember, WorkspaceRole
from src.models.file import IndexingStatus
from src.services import goclaw_client
from src.services import workspace as ws_svc

router = APIRouter(prefix="/admin", tags=["admin"])


def _require_owner(auth):
    """Raises 403 if the caller is not a superuser (no workspace context here —
    admin endpoints are global). For now we trust any authenticated user;
    tighten with a superuser flag on User when needed."""
    pass


@router.get("/files/stuck")
async def list_stuck_files(
    auth: CurrentAuth,
    older_than_minutes: int = Query(default=10, ge=1),
    db: AsyncSession = Depends(get_db),
):
    """List files stuck in 'processing' state — likely due to Parser crash.
    Recovery: POST /workspaces/{ws}/files/{id}/reindex on each returned file.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=older_than_minutes)
    result = await db.execute(
        select(File).where(
            File.indexing_status == IndexingStatus.processing,
            File.updated_at < cutoff,
        )
    )
    files = result.scalars().all()
    return [
        {
            "id": str(f.id),
            "workspace_id": str(f.workspace_id),
            "original_name": f.original_name,
            "updated_at": f.updated_at,
            "reindex_url": f"/v1/workspaces/{f.workspace_id}/files/{f.id}/reindex",
        }
        for f in files
    ]


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
    return [
        {"id": str(ws.id), "name": ws.name, "created_at": ws.created_at}
        for ws in workspaces
    ]


@router.get("/skills")
async def list_catalog_skills(auth: CurrentAuth):
    """List all skills available in the GoClaw catalog."""
    from src.core.config import settings as cfg

    if not cfg.goclaw_gateway_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GoClaw not configured",
        )
    return await goclaw_client.list_skills()


@router.post("/skills/sync")
async def sync_skills(auth: CurrentAuth):
    """
    Upload/update .md files from GOCLAW_SKILLS_DIR as GoClaw skill ZIPs.
    Idempotent — skips skills that already exist by name.
    Returns per-file status.
    """
    from src.core.config import settings as cfg

    if not cfg.goclaw_gateway_url:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="GoClaw not configured",
        )
    results = await goclaw_client.sync_skills_from_dir()
    return {"synced": results}


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
    import json

    from src.core.config import settings

    ws = await db.get(Workspace, workspace_id)
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Workspace not found"
        )

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
        json.dumps(
            {
                "api_key": goclaw["goclaw_api_key"],
                "agent_id": goclaw["goclaw_agent_id"],
                "agent_key": goclaw.get("goclaw_agent_key", ""),
            }
        ),
    )
    return {"status": "provisioned", "workspace_id": str(workspace_id), **goclaw}
