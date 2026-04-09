"""
Per-workspace skills.

Storage:  S3/Garage  {ws_id}/skills/{name}.md
Cache:    Redis       ws_skills:{ws_id}  — combined markdown, TTL 7 days
                      Rebuilt on every put/delete; Shell reads it at session start
                      and injects into the system prompt.

Name rules: alphanumeric + hyphens/underscores, max 80 chars, no path separators.
"""

from __future__ import annotations

import logging
import re

import redis.asyncio as aioredis
from fastapi import HTTPException, status

from src.services import s3

logger = logging.getLogger(__name__)

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
_SKILLS_TTL = 7 * 24 * 3600  # 7 days


def _s3_key(ws_id: str, name: str) -> str:
    return f"{ws_id}/skills/{name}.md"


def _s3_prefix(ws_id: str) -> str:
    return f"{ws_id}/skills/"


def _redis_key(ws_id: str) -> str:
    return f"ws_skills:{ws_id}"


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
        tail = key[len(prefix) :]
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
    """Create or replace a skill and rebuild the Redis cache."""
    _validate_name(name)
    if len(content) > 64 * 1024:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Skill content exceeds 64 KB limit",
        )
    await s3.upload_bytes(content.encode(), _s3_key(ws_id, name), "text/markdown")
    await _rebuild_cache(ws_id, redis)


async def delete_skill(ws_id: str, name: str, redis: aioredis.Redis) -> None:
    """Delete a skill and rebuild the Redis cache. Raises 404 if not found."""
    _validate_name(name)
    key = _s3_key(ws_id, name)
    if not await s3.object_exists(key):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found"
        )
    await s3.delete_object(key)
    await _rebuild_cache(ws_id, redis)


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


async def _rebuild_cache(ws_id: str, redis: aioredis.Redis) -> None:
    """Load all skills from S3 and write combined markdown to Redis."""
    names = await list_skills(ws_id)
    if not names:
        await redis.delete(_redis_key(ws_id))
        return

    parts = ["## Workspace Skills\n"]
    for name in names:
        try:
            key = _s3_key(ws_id, name)
            content = (await s3.download_bytes(key)).decode()
            parts.append(f"\n### {name}\n\n{content.strip()}\n")
        except Exception as exc:
            logger.warning(
                "Could not load skill '%s' for workspace %s: %s", name, ws_id, exc
            )

    combined = "\n".join(parts)
    await redis.setex(_redis_key(ws_id), _SKILLS_TTL, combined)
    logger.info("Rebuilt skills cache for workspace %s (%d skills)", ws_id, len(names))
