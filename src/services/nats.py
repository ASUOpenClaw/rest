"""
NATS JetStream client.

Publish helpers wait for a server ack (PubAck) so no messages are silently
dropped once NATS is reachable.  If NATS is unavailable at startup the client
degrades gracefully — publish calls log a warning and return without raising.
The three required streams (INDEXING, TRANSCRIPTION, CONVERSATIONS) are
created on connect if they don't already exist.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import nats
import nats.js
from nats.js.api import RetentionPolicy, StorageType, StreamConfig
from nats.js.errors import BadRequestError

logger = logging.getLogger(__name__)

_nc: nats.NATS | None = None
_js: nats.js.JetStreamContext | None = None

_STREAMS: list[StreamConfig] = [
    StreamConfig(
        name="INDEXING",
        subjects=["indexing.*"],
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
    ),
    StreamConfig(
        name="CONVERSATIONS",
        subjects=["conversation.*"],
        retention=RetentionPolicy.LIMITS,
        storage=StorageType.FILE,
    ),
]


async def _ensure_streams(js: nats.js.JetStreamContext) -> None:
    for cfg in _STREAMS:
        try:
            await js.add_stream(config=cfg)
            logger.info("NATS stream created: %s", cfg.name)
        except BadRequestError:
            pass  # stream already exists — that's fine


async def connect(url: str) -> None:
    global _nc, _js
    try:
        _nc = await nats.connect(url)
        _js = _nc.jetstream()
        await _ensure_streams(_js)
        logger.info("NATS connected: %s", url)
    except Exception as exc:
        logger.warning("NATS unavailable, running without messaging: %s", exc)
        _nc = None
        _js = None


def get_js() -> nats.js.JetStreamContext | None:
    return _js


async def close() -> None:
    global _nc, _js
    if _nc is not None:
        await _nc.drain()
        _nc = None
        _js = None


async def publish(subject: str, payload: dict[str, Any]) -> None:
    """
    Publish a message to a JetStream subject and wait for server ack.
    If NATS is not connected, logs a warning and returns without raising.
    Publish errors when connected are propagated to the caller.
    """
    if _js is None:
        logger.warning("NATS not connected — message to %s not delivered", subject)
        return
    await _js.publish(subject, json.dumps(payload).encode())


# ---------------------------------------------------------------------------
# Typed publish helpers
# ---------------------------------------------------------------------------


async def publish_index_job(
    *,
    job_id: str,
    job_type: str,
    workspace_id: str,
    file_id: str,
    s3_key: str | None = None,
    mime_type: str | None = None,
    original_name: str | None = None,
    metadata: dict | None = None,
) -> None:
    payload: dict[str, Any] = {
        "job_id": job_id,
        "type": job_type,
        "workspace_id": workspace_id,
        "file_id": file_id,
    }
    if s3_key is not None:
        payload["s3_key"] = s3_key
    if mime_type is not None:
        payload["mime_type"] = mime_type
    if original_name is not None:
        payload["original_name"] = original_name
    if metadata is not None:
        payload["metadata"] = metadata
    await publish("indexing.jobs", payload)
