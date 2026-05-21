"""
Background sync: REST DB ↔ GoClaw (via Shell WS RPC bridge).

Runs every GOCLAW_SYNC_INTERVAL_SECONDS seconds (default 300).

What it syncs per workspace:
  1. Sessions → Conversations: for every active GoClaw session, ensure a
     Conversation row exists in the REST DB with the matching goclaw_session_key.
  2. Tenant health: marks workspaces where Shell can't reach GoClaw
     (logged as warnings, no DB writes for health state yet).

The NATS subscriber already creates Conversation rows in real-time; this
sync fills gaps for sessions that were active before REST restarted, or for
sessions started through channels that bypass Shell (Telegram, cron, etc.).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.core.config import settings
from src.core.db import AsyncSessionLocal
from src.models import Conversation
from src.models.workspace import Workspace, WorkspaceMember, WorkspaceRole
from src.services import shell_client

logger = logging.getLogger(__name__)

_running = False


async def start() -> None:
    """Launch the sync loop as a background asyncio task."""
    global _running
    if _running:
        return
    _running = True
    asyncio.create_task(_loop(), name="goclaw-sync")
    logger.info(
        "GoClaw sync task started (interval=%ds)", settings.goclaw_sync_interval_seconds
    )


async def _loop() -> None:
    while True:
        await asyncio.sleep(settings.goclaw_sync_interval_seconds)
        try:
            await _sync_all()
        except Exception:
            logger.exception("goclaw_sync: unhandled error in sync cycle")


async def _sync_all() -> None:
    async with AsyncSessionLocal() as db:
        from sqlalchemy import func

        result = await db.execute(
            select(Workspace).where(
                func.jsonb_typeof(Workspace.config["goclaw_api_key"]) == "string"
            )
        )
        workspaces = list(result.scalars().all())

    logger.debug("goclaw_sync: syncing %d provisioned workspaces", len(workspaces))
    for ws in workspaces:
        try:
            await _sync_workspace(ws)
        except Exception:
            logger.exception("goclaw_sync: failed for workspace %s", ws.id)


async def sync_workspace(ws: Workspace) -> int:
    """Sync GoClaw sessions → REST conversations for one workspace. Returns session count."""
    ws_id = str(ws.id)

    # --- 1. Fetch sessions from Shell ------------------------------------------
    try:
        sessions = await shell_client.list_sessions(ws_id)
    except Exception as exc:
        logger.warning("goclaw_sync: workspace %s — Shell unreachable: %s", ws_id, exc)
        return 0

    if not sessions:
        return 0

    # --- 2. Resolve user_id → UUID from session key ----------------------------
    # Default session key format: "user-{uuid}"
    # Custom keys: store under workspace creator as fallback.
    creator_id: uuid.UUID | None = ws.created_by  # may be None on very old rows

    # --- 3. Upsert Conversation for each session --------------------------------
    async with AsyncSessionLocal() as db:
        for session in sessions:
            session_key: str | None = session.get("key") or session.get("sessionKey")
            if not session_key:
                continue

            user_id = _parse_user_id(session_key) or creator_id
            if user_id is None:
                continue

            await _ensure_conversation(db, ws.id, user_id, session_key, session)

        await db.commit()

    logger.debug("goclaw_sync: workspace %s — synced %d sessions", ws_id, len(sessions))
    return len(sessions)


async def _sync_workspace(ws: Workspace) -> None:
    await sync_workspace(ws)


async def _ensure_conversation(
    db,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    session_key: str,
    session_data: dict,
) -> None:
    """Create a Conversation row for this session if one doesn't exist yet."""
    existing = await db.scalar(
        select(Conversation).where(
            Conversation.workspace_id == workspace_id,
            Conversation.goclaw_session_key == session_key,
        )
    )
    if existing is not None:
        # Update last_message_at if GoClaw session reports more recent activity
        goclaw_updated = _parse_timestamp(
            session_data.get("updatedAt") or session_data.get("lastMessageAt")
        )
        if goclaw_updated and (
            existing.last_message_at is None
            or goclaw_updated > existing.last_message_at
        ):
            existing.last_message_at = goclaw_updated
        return

    conv = Conversation(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        user_id=user_id,
        goclaw_session_key=session_key,
        # Auto-title from session metadata if available
        title=_extract_title(session_data, session_key),
        last_message_at=_parse_timestamp(
            session_data.get("updatedAt") or session_data.get("lastMessageAt")
        ),
    )
    db.add(conv)
    try:
        await db.flush()
        logger.info(
            "goclaw_sync: created conversation %s for session_key=%s ws=%s",
            conv.id,
            session_key,
            workspace_id,
        )
    except IntegrityError:
        await db.rollback()
        # Another task or NATS subscriber beat us to it — that's fine.


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_user_id(session_key: str) -> uuid.UUID | None:
    """Extract user UUID from default session key format "user-{uuid}"."""
    if session_key.startswith("user-"):
        try:
            return uuid.UUID(session_key[5:])
        except ValueError:
            pass
    return None


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(UTC)
    except (ValueError, AttributeError):
        return None


def _extract_title(session_data: dict, session_key: str) -> str | None:
    """Try to get a human-readable title from session metadata."""
    title = session_data.get("title") or session_data.get("preview")
    if title:
        return str(title)[:60]
    # Fall back to session key itself as the title
    return session_key[:60]
