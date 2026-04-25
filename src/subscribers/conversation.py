"""
Subscriber: conversation.{workspace_id}

Consumes conversation dumps published by the Shell and writes to:
  - conversations (upsert by goclaw_session_key)
  - conversation_messages (insert)

The Shell now publishes a top-level `session_key` field on every message.
The subscriber uses (workspace_id, goclaw_session_key) as the stable identity
for a conversation — creating a new row the first time a session is seen.

Legacy messages that carry a `conversation_id` UUID in the body are still handled
for backwards compatibility.

Parse errors are always ACKed. DB errors cause NACK for redelivery.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from datetime import UTC, datetime

import nats.js
from nats.aio.msg import Msg
from sqlalchemy import select
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

    try:
        workspace_id = uuid.UUID(workspace_id_raw)
        user_id = uuid.UUID(user_id_raw)
    except ValueError as exc:
        logger.error("conversation: bad workspace_id/user_id: %s", exc)
        await msg.ack()
        return

    # Resolve conversation_id: prefer session_key lookup, fall back to explicit UUID
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
                await _handle_response(db, conversation_id, workspace_id, user_id, body)
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
    """Find or create a Conversation for this (workspace, session_key) pair."""
    conv = await db.scalar(
        select(Conversation).where(
            Conversation.workspace_id == workspace_id,
            Conversation.goclaw_session_key == session_key,
        )
    )
    if conv is not None:
        return conv.id

    # New session — create conversation row
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
        # Race condition: another message created it concurrently — reload
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

    if conv.title is None:
        messages: list[dict] = body.get("messages", [])
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content") or ""
                if isinstance(content, list):
                    content = " ".join(
                        p.get("text", "") for p in content if isinstance(p, dict)
                    )
                conv.title = str(content)[:60] or None
                break

    messages: list[dict] = body.get("messages", [])
    last_user_content: str | None = None
    for m in reversed(messages):
        if m.get("role") == "user":
            raw_content = m.get("content") or ""
            if isinstance(raw_content, list):
                last_user_content = " ".join(
                    p.get("text", "") for p in raw_content if isinstance(p, dict)
                )
            else:
                last_user_content = str(raw_content)
            break

    db.add(
        ConversationMessage(
            conversation_id=conversation_id,
            role=MessageRole.user,
            content=last_user_content,
            raw=body,
        )
    )


async def _handle_response(db, conversation_id, workspace_id, user_id, body) -> None:
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        logger.warning(
            "conversation: response for unknown conversation %s", conversation_id
        )
        return

    choices: list[dict] = body.get("choices", [])
    first_choice = choices[0] if choices else {}
    message = first_choice.get("message", {})
    content: str | None = message.get("content")
    finish_reason: str | None = first_choice.get("finish_reason")
    model: str | None = body.get("model")
    usage: dict = body.get("usage", {})

    db.add(
        ConversationMessage(
            conversation_id=conversation_id,
            role=MessageRole.assistant,
            content=content,
            model=model,
            finish_reason=finish_reason,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            total_tokens=usage.get("total_tokens"),
            raw=body,
        )
    )

    conv.message_count = (conv.message_count or 0) + 1
    conv.last_message_at = datetime.now(UTC)

    if conv.message_count >= settings.rag_index_threshold:
        await _trigger_conversation_index(db, conv)


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
