"""
Subscriber: indexing.results

Consumes indexing job outcomes from the Indexing service and updates:
  - files.indexing_status / indexed_chunks / indexing_error / last_indexed_at
  - conversations.rag_indexed_at  (for virtual conversation files)

On parse error: ACK and log — never block the stream.
On DB error: NACK so the server redelivers.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

import nats.js
from nats.aio.msg import Msg
from sqlalchemy import select

from src.core.db import AsyncSessionLocal
from src.models import File
from src.models.file import IndexingStatus

logger = logging.getLogger(__name__)

_DURABLE = "rest-indexing-results"


async def _handle(msg: Msg) -> None:
    try:
        data = json.loads(msg.data)
    except Exception as exc:
        logger.error("indexing.results: failed to parse message: %s", exc)
        await msg.ack()
        return

    file_id_raw: str = data.get("file_id", "")
    status_raw: str = data.get("status", "")
    indexed_chunks: int = data.get("indexed_chunks") or 0
    error: str | None = data.get("error")
    completed_at_raw: str | None = data.get("completed_at")

    completed_at: datetime | None = None
    if completed_at_raw:
        try:
            completed_at = datetime.fromisoformat(completed_at_raw.rstrip("Z")).replace(
                tzinfo=UTC
            )
        except ValueError:
            pass

    try:
        async with AsyncSessionLocal() as db:
            # Virtual conversation file: "conversation:{conversation_id}"
            if file_id_raw.startswith("conversation:"):
                conv_id_str = file_id_raw.split(":", 1)[1]
                try:
                    conv_id = uuid.UUID(conv_id_str)
                except ValueError:
                    logger.error(
                        "indexing.results: bad conversation id %s", conv_id_str
                    )
                    await msg.ack()
                    return

                from src.models import Conversation

                conv = await db.get(Conversation, conv_id)
                if conv and status_raw == "completed":
                    conv.rag_indexed_at = completed_at or datetime.now(UTC)
                    await db.commit()
            else:
                # Regular file
                try:
                    file_id = uuid.UUID(file_id_raw)
                except ValueError:
                    logger.error("indexing.results: bad file_id %s", file_id_raw)
                    await msg.ack()
                    return

                file = await db.get(File, file_id)
                if file is None:
                    logger.warning(
                        "indexing.results: file %s not found, skipping", file_id
                    )
                    await msg.ack()
                    return

                file.indexing_status = (
                    IndexingStatus.completed
                    if status_raw == "completed"
                    else IndexingStatus.failed
                )
                file.indexed_chunks = indexed_chunks
                file.indexing_error = error
                file.last_indexed_at = (completed_at or datetime.now(UTC)).isoformat()
                await db.commit()

    except Exception as exc:
        logger.error("indexing.results: DB error, will redeliver: %s", exc)
        await msg.nak()
        return

    await msg.ack()


async def start(js: nats.js.JetStreamContext) -> nats.js.JetStreamContext:
    await js.subscribe(
        "indexing.results",
        durable=_DURABLE,
        cb=_handle,
    )
    logger.info("Indexing results subscriber started (durable=%s)", _DURABLE)
    return js
