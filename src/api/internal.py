"""
Internal service-to-service endpoints.

Protected by X-Service-Key (GoClaw webhooks).
Not mounted under /v1 — registered at /api prefix in main.py.
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.db import get_db
from src.models import Conversation, ConversationMessage
from src.models.conversation import MessageRole
from src.models.file import File
from src.services.nats import publish_index_job
from src.services.s3 import upload_fileobj

router = APIRouter(prefix="/api/internal", tags=["internal"])
logger = logging.getLogger(__name__)

_service_key_header = APIKeyHeader(name="X-Service-Key", auto_error=False)


async def _require_goclaw_webhook_key(
    key: Annotated[str | None, Security(_service_key_header)] = None,
) -> None:
    if not key or not settings.goclaw_webhook_secret:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="missing service key"
        )
    if not secrets.compare_digest(key, settings.goclaw_webhook_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid service key"
        )


# ──────────────────────────────────────────────────────────────────────────────
# GoClaw webhooks
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/turn-end", status_code=status.HTTP_204_NO_CONTENT)
@router.post("/turn-event", status_code=status.HTTP_204_NO_CONTENT)
async def turn_event(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_goclaw_webhook_key),
) -> None:
    """
    Receives EventTurnEnd from GoClaw after every completed LLM turn.
    Saves user + assistant messages to our conversation history tables.
    Replaces the NATS CONVERSATIONS subscriber.
    """
    body: dict[str, Any] = await request.json()

    tenant_id_raw: str = body.get("TenantID", "")
    user_id_raw: str = body.get("UserID", "")
    session_key: str | None = body.get("SessionID") or None
    turn: dict[str, Any] = body.get("TurnEnd", {})

    if not session_key or not tenant_id_raw:
        return  # nothing to save

    try:
        workspace_id = uuid.UUID(tenant_id_raw)
    except ValueError:
        logger.error("turn-event: bad tenant_id %s", tenant_id_raw)
        return

    user_id: uuid.UUID | None = None
    if user_id_raw:
        try:
            user_id = uuid.UUID(user_id_raw)
        except ValueError:
            pass

    user_message: str = turn.get("user_message", "")
    assistant_text: str = turn.get("assistant_text", "")
    usage: dict[str, Any] = turn.get("usage", {})

    try:
        async with db.begin_nested():
            conversation_id = await _upsert_conversation(
                db, workspace_id, user_id, session_key
            )

        if user_message:
            db.add(
                ConversationMessage(
                    conversation_id=conversation_id,
                    role=MessageRole.user,
                    content=user_message,
                    raw={"content": user_message},
                )
            )

        if assistant_text:
            db.add(
                ConversationMessage(
                    conversation_id=conversation_id,
                    role=MessageRole.assistant,
                    content=assistant_text,
                    raw={"content": assistant_text, "usage": usage},
                )
            )

        # Update conversation metadata
        conv = await db.get(Conversation, conversation_id)
        if conv is not None:
            if conv.title is None and user_message:
                conv.title = user_message[:60]
            conv.message_count = (conv.message_count or 0) + 1
            conv.last_message_at = datetime.now(UTC)

            from src.core.config import settings as _s

            if (conv.message_count or 0) >= _s.rag_index_threshold:
                await _trigger_conversation_index(db, conv)

        await db.commit()
    except Exception as exc:
        logger.error("turn-event: DB error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="internal error")


@router.post("/file-created", status_code=status.HTTP_204_NO_CONTENT)
async def file_created(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(_require_goclaw_webhook_key),
) -> None:
    """
    Receives S3 file-created webhook from GoClaw after agent writes a file.
    Creates a File record in our DB and triggers indexing.
    Replaces the publish_workspace_file MCP tool.
    """
    body: dict[str, Any] = await request.json()

    tenant_id_raw: str = body.get("tenant_id", "")
    user_id_raw: str = body.get("user_id", "")
    s3_key: str = body.get("s3_key", "")
    path: str = body.get("path", "")
    size: int = body.get("size", 0)
    mime_type: str = body.get("mime_type", "application/octet-stream")

    if not tenant_id_raw or not s3_key:
        return

    try:
        workspace_id = uuid.UUID(tenant_id_raw)
    except ValueError:
        logger.error("file-created: bad tenant_id %s", tenant_id_raw)
        return

    user_id: uuid.UUID | None = None
    if user_id_raw:
        try:
            user_id = uuid.UUID(user_id_raw)
        except ValueError:
            pass

    try:
        file = File(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            uploaded_by=user_id,
            original_name=path.rsplit("/", 1)[-1],
            s3_key=s3_key,
            size_bytes=size,
            mime_type=mime_type,
        )
        db.add(file)
        await db.commit()
        await db.refresh(file)
        await publish_index_job(str(workspace_id), str(file.id), s3_key, mime_type)
    except Exception as exc:
        logger.error("file-created: error: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="internal error")


async def _upsert_conversation(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    session_key: str,
) -> uuid.UUID:
    conv = await db.scalar(
        select(Conversation).where(
            Conversation.workspace_id == workspace_id,
            Conversation.goclaw_session_key == session_key,
        )
    )
    if conv is not None:
        return conv.id

    conv = Conversation(
        id=uuid.uuid4(),
        workspace_id=workspace_id,
        user_id=user_id,
        goclaw_session_key=session_key,
    )
    db.add(conv)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        conv = await db.scalar(
            select(Conversation).where(
                Conversation.workspace_id == workspace_id,
                Conversation.goclaw_session_key == session_key,
            )
        )
        if conv is None:
            raise
    return conv.id


async def _trigger_conversation_index(db: AsyncSession, conv: Conversation) -> None:
    """Upload conversation transcript to S3 and enqueue RAG indexing."""
    import io

    result = await db.execute(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conv.id)
        .order_by(ConversationMessage.created_at)
    )
    messages = result.scalars().all()
    lines = [f"[{m.role.value.upper()}]\n{m.content or ''}\n" for m in messages]
    transcript = "\n".join(lines)

    s3_key = f"{conv.workspace_id}/conversations/{conv.id}.txt"
    try:
        await upload_fileobj(io.BytesIO(transcript.encode()), s3_key, "text/plain")
    except Exception as exc:
        logger.error("conversation-index: upload failed: %s", exc)
        return

    try:
        await publish_index_job(
            str(conv.workspace_id), str(conv.id), s3_key, "text/plain"
        )
    except Exception as exc:
        logger.error("conversation-index: nats publish failed: %s", exc)
