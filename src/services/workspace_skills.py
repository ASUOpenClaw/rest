"""
Per-workspace skills.

Storage:  S3/Garage  {ws_id}/skills/{name}.md
GoClaw:   Agent context file  SKILL_{name}.md  (set via Shell WS-RPC)
          GoClaw injects context files into the system prompt on every turn,
          so no per-session injection in Shell is needed.

Name rules: alphanumeric + hyphens/underscores, max 80 chars, no path separators.
"""

from __future__ import annotations

import json
import logging
import re

import redis.asyncio as aioredis
from fastapi import HTTPException, status

from src.services import s3, shell_client

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")


def _s3_key(ws_id: str, name: str) -> str:
    return f"{ws_id}/skills/{name}.md"


def _s3_prefix(ws_id: str) -> str:
    return f"{ws_id}/skills/"


def _goclaw_filename(name: str) -> str:
    return f"SKILL_{name}.md"


def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Skill name must be 1-80 alphanumeric/hyphen/underscore characters",
        )


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


async def list_skills(ws_id: str) -> list[str]:
    """Return skill names (sorted) for this workspace."""
    prefix = _s3_prefix(ws_id)
    keys = await s3.list_objects_prefix(prefix)
    names = []
    for key in keys:
        tail = key[len(prefix):]
        if tail.endswith(".md"):
            names.append(tail[:-3])
    return sorted(names)


async def get_skill(ws_id: str, name: str) -> str:
    """Return skill .md content. Raises 404 if not found."""
    _validate_name(name)
    key = _s3_key(ws_id, name)
    if not await s3.object_exists(key):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found"
        )
    data = await s3.download_bytes(key)
    return data.decode()


async def put_skill(ws_id: str, name: str, content: str, redis: aioredis.Redis) -> None:
    """Create or replace a skill and push it to the GoClaw agent context."""
    _validate_name(name)
    if len(content) > 64 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Skill content exceeds 64 KB limit",
        )
    await s3.upload_bytes(content.encode(), _s3_key(ws_id, name), "text/markdown")
    await _sync_to_goclaw(ws_id, name, content, redis)


async def delete_skill(ws_id: str, name: str, redis: aioredis.Redis) -> None:
    """Delete a skill from S3 and clear it from the GoClaw agent context."""
    _validate_name(name)
    key = _s3_key(ws_id, name)
    if not await s3.object_exists(key):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found"
        )
    await s3.delete_object(key)
    await _sync_to_goclaw(ws_id, name, "", redis)


# ---------------------------------------------------------------------------
# GoClaw sync
# ---------------------------------------------------------------------------


async def _sync_to_goclaw(
    ws_id: str, name: str, content: str, redis: aioredis.Redis
) -> None:
    """Push (or clear) a skill as a GoClaw agent context file. Non-fatal."""
    try:
        raw = await redis.get(f"ws_creds:{ws_id}")
        if not raw:
            logger.warning(
                "ws_creds not found for workspace %s — skill '%s' not synced to GoClaw",
                ws_id,
                name,
            )
            return
        creds = json.loads(raw)
        agent_key = creds.get("agent_id") or creds.get("agent_key", "")
        if not agent_key:
            logger.warning(
                "No agent_key in ws_creds for workspace %s — skill '%s' not synced",
                ws_id,
                name,
            )
            return
        await shell_client.set_agent_file(ws_id, agent_key, _goclaw_filename(name), content)
        logger.info(
            "Synced skill '%s' to GoClaw agent %s for workspace %s (len=%d)",
            name,
            agent_key,
            ws_id,
            len(content),
        )
    except Exception as exc:
        logger.warning(
            "Failed to sync skill '%s' to GoClaw for workspace %s: %s", name, ws_id, exc
        )
