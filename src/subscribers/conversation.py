"""
Subscriber: conversation.{workspace_id}

Consumes conversation dumps published by the Shell and writes to:
  - conversations (upsert by goclaw_session_key)
  - conversation_messages (insert)

On direction=response the envelope may include:
  - events: list of structured turn events (tool_call, tool_result, thinking, chunk)
  - goclaw_history: full chat.history payload from GoClaw fetched after run.completed

Sync logic: after saving the response turn we compare our local message count
against len(goclaw_history). If they differ the entire conversation is rewritten
from GoClaw's authoritative history.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from datetime import UTC, datetime

import nats.js
from nats.aio.msg import Msg
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from src.core.config import settings
from src.core.db import AsyncSessionLocal
from src.models import Conversation, ConversationMessage
from src.models.conversation import MessageRole

logger = logging.getLogger(__name__)

_DURABLE = "rest-conversation-dump"


async def _handle(msg: Msg) -> None:
    try:
        event = json.loads(msg.data)
    except Exception as exc:
        logger.error("conversation: failed to parse message: %s", exc)
        await msg.ack()
        return

    direction: str = event.get("direction", "")
    workspace_id_raw: str = event.get("workspace_id", "")
    user_id_raw: str = event.get("user_id", "")
    session_key: str | None = event.get("session_key") or None
    body: dict = event.get("body", {})
    turn_events: list[dict] = event.get("events", [])
    goclaw_history = event.get("goclaw_history")  # None or list or {"messages": [...]}

    try:
        workspace_id = uuid.UUID(workspace_id_raw)
        user_id = uuid.UUID(user_id_raw)
    except ValueError as exc:
        logger.error("conversation: bad workspace_id/user_id: %s", exc)
        await msg.ack()
        return

    conversation_id: uuid.UUID | None = None
    if not session_key:
        conv_id_raw = body.get("conversation_id") or body.get("x_lab_metadata", {}).get(
            "conversation_id"
        )
        if not conv_id_raw:
            logger.warning("conversation: no session_key or conversation_id, skipping")
            await msg.ack()
            return
        try:
            conversation_id = uuid.UUID(conv_id_raw)
        except ValueError:
            logger.error("conversation: bad conversation_id %s", conv_id_raw)
            await msg.ack()
            return

    try:
        async with AsyncSessionLocal() as db:
            if session_key:
                conversation_id = await _upsert_by_session_key(
                    db, workspace_id, user_id, session_key
                )
            if conversation_id is None:
                await msg.ack()
                return
            if direction == "request":
                await _handle_request(db, conversation_id, workspace_id, user_id, body)
            elif direction == "response":
                await _handle_response(
                    db,
                    conversation_id,
                    workspace_id,
                    user_id,
                    body,
                    turn_events,
                    goclaw_history,
                )
            else:
                logger.warning("conversation: unknown direction %s", direction)
            await db.commit()
    except Exception as exc:
        logger.error("conversation: DB error, will redeliver: %s", exc)
        await msg.nak()
        return

    await msg.ack()


async def _upsert_by_session_key(
    db,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
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


async def _handle_request(db, conversation_id, workspace_id, user_id, body) -> None:
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        conv = Conversation(
            id=conversation_id,
            workspace_id=workspace_id,
            user_id=user_id,
        )
        db.add(conv)
        await db.flush()

    content: str = body.get("content") or ""
    if isinstance(content, list):
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))

    if conv.title is None:
        conv.title = str(content)[:60] or None

    db.add(
        ConversationMessage(
            conversation_id=conversation_id,
            role=MessageRole.user,
            content=content or None,
            raw=body,
        )
    )


async def _handle_response(
    db,
    conversation_id,
    workspace_id,
    user_id,
    body,
    turn_events: list[dict],
    goclaw_history,
) -> None:
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        logger.warning("conversation: response for unknown conversation %s", conversation_id)
        return

    history_messages = _extract_history_messages(goclaw_history) if goclaw_history is not None else []

    if history_messages:
        # GoClaw history available — always use it as the source of truth.
        await _rewrite_from_history(db, conversation_id, history_messages)
    elif turn_events:
        # Best-effort from structured events (fallback when chat.history failed).
        await _save_turn_events(db, conversation_id, body, turn_events)
    else:
        # Legacy: no events and no history — save plain assistant message.
        db.add(
            ConversationMessage(
                conversation_id=conversation_id,
                role=MessageRole.assistant,
                content=body.get("content") or None,
                raw=body,
            )
        )

    conv.message_count = (conv.message_count or 0) + 1
    conv.last_message_at = datetime.now(UTC)

    if conv.message_count >= settings.rag_index_threshold:
        await _trigger_conversation_index(db, conv)


async def _save_turn_events(
    db, conversation_id: uuid.UUID, body: dict, events: list[dict]
) -> None:
    """
    Convert structured turn events to ConversationMessage rows.

    Groups:
      thinking   → assistant message with raw.type=thinking
      tool_call  → assistant message with raw.type=tool_call
      tool_result→ tool message
      chunk      → accumulated into one final assistant message
    """
    text_chunks: list[str] = []

    for ev in events:
        ev_type = ev.get("type")
        if ev_type == "thinking":
            db.add(
                ConversationMessage(
                    conversation_id=conversation_id,
                    role=MessageRole.assistant,
                    content=ev.get("text") or None,
                    raw=ev,
                )
            )
        elif ev_type == "tool_call":
            db.add(
                ConversationMessage(
                    conversation_id=conversation_id,
                    role=MessageRole.assistant,
                    content=None,
                    raw=ev,
                )
            )
        elif ev_type == "tool_result":
            db.add(
                ConversationMessage(
                    conversation_id=conversation_id,
                    role=MessageRole.tool,
                    content=ev.get("content") or None,
                    raw=ev,
                )
            )
        elif ev_type == "chunk":
            text_chunks.append(ev.get("text", ""))

    # Final assembled text
    assembled = "".join(text_chunks)
    db.add(
        ConversationMessage(
            conversation_id=conversation_id,
            role=MessageRole.assistant,
            content=assembled or None,
            raw={**body, "assembled": True},
        )
    )


def _extract_history_messages(goclaw_history) -> list[dict]:
    """Normalise chat.history payload to a flat list of message dicts."""
    if isinstance(goclaw_history, list):
        return goclaw_history
    if isinstance(goclaw_history, dict):
        for key in ("messages", "history", "items"):
            if isinstance(goclaw_history.get(key), list):
                return goclaw_history[key]
    return []


def _map_role(role_str: str) -> MessageRole:
    mapping = {
        "user": MessageRole.user,
        "assistant": MessageRole.assistant,
        "tool": MessageRole.tool,
        "system": MessageRole.system,
        "function": MessageRole.tool,
    }
    return mapping.get(role_str, MessageRole.assistant)


async def _rewrite_from_history(
    db, conversation_id: uuid.UUID, history_messages: list[dict]
) -> None:
    """Delete all local messages and re-insert from GoClaw's authoritative history."""
    await db.execute(
        delete(ConversationMessage).where(
            ConversationMessage.conversation_id == conversation_id
        )
    )
    for msg in history_messages:
        role = _map_role(msg.get("role", "assistant"))
        content = msg.get("content")
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict):
                    parts.append(p.get("text") or p.get("thinking") or "")
            content = " ".join(parts)
        # GoClaw stores thinking as a top-level field when content is empty
        if not content:
            content = msg.get("thinking") or ""
        # For tool-call-only turns attach a compact summary so content is non-empty
        if not content:
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                names = ", ".join(tc.get("name", "?") for tc in tool_calls)
                content = f"[tool_calls: {names}]"
        db.add(
            ConversationMessage(
                conversation_id=conversation_id,
                role=role,
                content=content or None,
                raw=msg,
            )
        )


async def _trigger_conversation_index(db, conv: Conversation) -> None:
    result = await db.execute(
        select(ConversationMessage)
        .where(ConversationMessage.conversation_id == conv.id)
        .order_by(ConversationMessage.created_at)
    )
    messages = result.scalars().all()

    lines: list[str] = []
    for m in messages:
        role_label = m.role.value.upper()
        lines.append(f"[{role_label}]\n{m.content or ''}\n")
    transcript = "\n".join(lines)

    s3_key = f"{conv.workspace_id}/conversations/{conv.id}.txt"
    try:
        from src.services.s3 import upload_fileobj

        await upload_fileobj(io.BytesIO(transcript.encode()), s3_key, "text/plain")
    except Exception as exc:
        logger.error("conversation: failed to upload transcript: %s", exc)
        return

    try:
        from src.services.nats import publish_index_job

        await publish_index_job(
            job_id=str(uuid.uuid4()),
            job_type="index",
            workspace_id=str(conv.workspace_id),
            file_id=f"conversation:{conv.id}",
            s3_key=s3_key,
            mime_type="text/plain",
            original_name=f"conversation_{conv.title or conv.id}.txt",
            metadata={
                "source": "conversation",
                "conversation_id": str(conv.id),
                "user_id": str(conv.user_id),
            },
        )
    except Exception as exc:
        logger.error("conversation: failed to publish indexing job: %s", exc)


async def start(js: nats.js.JetStreamContext) -> None:
    await js.subscribe(
        "conversation.*",
        durable=_DURABLE,
        cb=_handle,
    )
    logger.info("Conversation subscriber started (durable=%s)", _DURABLE)
