"""
Subscriber: conversation.{workspace_id}

Consumes conversation dumps published by the Rust proxy and writes to:
  - conversations (upsert)
  - conversation_messages (insert)

Direction=request:
  - Upsert Conversation (id from body.conversation_id, workspace_id, user_id)
  - Set title = first 60 chars of last user message if title is None
  - Insert ConversationMessage(role=user, content, raw=body)

Direction=response:
  - Insert ConversationMessage(role=assistant, content, model, usage, raw=body)
  - Update conversation.message_count + 1, last_message_at
  - If message_count >= RAG_INDEX_THRESHOLD: upload transcript to S3, publish indexing.jobs

Parse errors are always ACKed so they never block the stream.
DB errors cause NACK for redelivery.
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
    body: dict = event.get("body", {})

    try:
        workspace_id = uuid.UUID(workspace_id_raw)
        user_id = uuid.UUID(user_id_raw)
    except ValueError as exc:
        logger.error("conversation: bad workspace_id/user_id: %s", exc)
        await msg.ack()
        return

    conversation_id_raw: str | None = body.get("conversation_id") or body.get(
        "x_lab_metadata", {}
    ).get("conversation_id")
    if not conversation_id_raw:
        logger.warning("conversation: no conversation_id in message, skipping")
        await msg.ack()
        return

    try:
        conversation_id = uuid.UUID(conversation_id_raw)
    except ValueError:
        logger.error("conversation: bad conversation_id %s", conversation_id_raw)
        await msg.ack()
        return

    try:
        async with AsyncSessionLocal() as db:
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


async def _handle_request(db, conversation_id, workspace_id, user_id, body) -> None:
    # Upsert conversation row
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        conv = Conversation(
            id=conversation_id,
            workspace_id=workspace_id,
            user_id=user_id,
        )
        db.add(conv)
        await db.flush()

    # Auto-title from first user message content
    if conv.title is None:
        messages: list[dict] = body.get("messages", [])
        for m in reversed(messages):
            if m.get("role") == "user":
                content = m.get("content") or ""
                if isinstance(content, list):
                    # content may be a list of parts
                    content = " ".join(
                        p.get("text", "") for p in content if isinstance(p, dict)
                    )
                conv.title = str(content)[:60] or None
                break

    # Extract last user message
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

    # Trigger RAG indexing when threshold is reached
    if conv.message_count >= settings.rag_index_threshold:
        await _trigger_conversation_index(db, conv)


async def _trigger_conversation_index(db, conv: Conversation) -> None:
    from sqlalchemy import select as sa_select

    # Assemble transcript
    result = await db.execute(
        sa_select(ConversationMessage)
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

        await upload_fileobj(
            io.BytesIO(transcript.encode()),
            s3_key,
            "text/plain",
        )
    except Exception as exc:
        logger.error("conversation: failed to upload transcript to S3: %s", exc)
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
    # Subscribe to all workspace conversation subjects with a single wildcard consumer
    await js.subscribe(
        "conversation.*",
        durable=_DURABLE,
        cb=_handle,
    )
    logger.info("Conversation subscriber started (durable=%s)", _DURABLE)
