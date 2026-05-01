"""
Internal service-to-service endpoints.

Protected by X-Shell-Service-Key (shared with Shell).
Not mounted under /v1 — registered at /api prefix in main.py.
"""

import json
import secrets
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.db import get_db
from src.core.redis import get_redis
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole

router = APIRouter(prefix="/api/internal", tags=["internal"])

_shell_key_header = APIKeyHeader(name="X-Shell-Service-Key", auto_error=False)


async def _require_shell_key(
    key: Annotated[str | None, Security(_shell_key_header)] = None,
) -> None:
    if not key or not settings.shell_service_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing service key"
        )
    if not secrets.compare_digest(key, settings.shell_service_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid service key"
        )


@router.get(
    "/workspaces/{ws_id}/creds",
    dependencies=[Depends(_require_shell_key)],
    summary="Return (and re-cache) GoClaw credentials for a workspace",
)
async def get_workspace_creds(
    ws_id: str,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
) -> dict:
    redis_key = f"ws_creds:{ws_id}"

    # Fast path — already cached
    if redis is not None:
        cached = await redis.get(redis_key)
        if cached:
            return json.loads(cached)

    # Slow path — rebuild from Postgres
    result = await db.execute(select(Workspace).where(Workspace.id == ws_id))
    ws = result.scalar_one_or_none()
    if ws is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found"
        )

    cfg = ws.config or {}
    api_key = cfg.get("goclaw_api_key")
    # agent_id in ws_creds must be the slug (agent_key), not the internal UUID.
    agent_id = cfg.get("goclaw_agent_key") or cfg.get("goclaw_agent_id", "")
    mcp_token = cfg.get("goclaw_mcp_service_token", "")

    if not api_key or not agent_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="workspace not provisioned (goclaw credentials missing from config)",
        )

    owner_result = await db.execute(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == ws.id,
            WorkspaceMember.role == WorkspaceRole.owner,
        )
    )
    owner = owner_result.scalar_one_or_none()

    creds = {
        "api_key": api_key,
        "agent_id": agent_id,
        "mcp_service_token": mcp_token,
        "owner_user_id": str(owner.user_id) if owner else "",
    }

    if redis is not None:
        await redis.setex(redis_key, 86400, json.dumps(creds))

    return creds
