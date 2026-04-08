"""
Subscriber: transcription.results

Consumes results published by the Parser/Transcriber service and:
  - On success: uploads transcript text to S3, creates File + Transcription records,
                publishes indexing job, updates TranscriptionTask
  - On failure: marks TranscriptionTask as failed

On parse error: ACK and log — never block the stream.
On DB/S3 error: NACK for redelivery.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime

import nats.js
from nats.aio.msg import Msg

from src.core.db import AsyncSessionLocal
from src.models import File, Transcription, TranscriptionTask
from src.models.file import IndexingStatus
from src.models.transcription import TranscriptionStatus
from src.services import nats as nats_svc
from src.services import s3 as s3_svc

logger = logging.getLogger(__name__)

_DURABLE = "rest-transcription-results"


async def _handle(msg: Msg) -> None:
    try:
        data = json.loads(msg.data)
    except Exception as exc:
        logger.error("transcription.results: failed to parse message: %s", exc)
        await msg.ack()
        return

    task_id_raw: str = data.get("task_id", "")
    status_raw: str = data.get("status", "")
    result: dict | None = data.get("result")
    error: str | None = data.get("error")
    processing_time_sec: float | None = data.get("processing_time_sec")
    workspace_id_raw: str = data.get("workspace_id", "")
    audio_file_id_raw: str = data.get("audio_file_id", "")
    filename: str = data.get("filename", "transcript.txt")
    requested_by_raw: str | None = data.get("requested_by")

    try:
        task_id = uuid.UUID(task_id_raw)
        workspace_id = uuid.UUID(workspace_id_raw)
        audio_file_id = uuid.UUID(audio_file_id_raw)
        requested_by = uuid.UUID(requested_by_raw) if requested_by_raw else None
    except ValueError as exc:
        logger.error("transcription.results: bad UUID in message: %s", exc)
        await msg.ack()
        return

    try:
        async with AsyncSessionLocal() as db:
            task = await db.get(TranscriptionTask, task_id)
            if task is None:
                logger.warning("transcription.results: task %s not found", task_id)
                await msg.ack()
                return

            if status_raw == "failed" or not result:
                task.status = TranscriptionStatus.failed
                task.error = error or "Transcription failed"
                task.completed_at = datetime.now(UTC)
                await db.commit()
                await msg.ack()
                return

            # Upload transcript text as a workspace File.
            transcript_text = result.get("text", "")
            transcript_filename = f"{filename.rsplit('.', 1)[0]}_transcript.txt"
            transcript_s3_key = f"{workspace_id}/transcriptions/{task_id}.txt"
            transcript_bytes = transcript_text.encode("utf-8")

            await s3_svc.upload_bytes(transcript_bytes, transcript_s3_key, "text/plain")

            transcript_file = File(
                workspace_id=workspace_id,
                original_name=transcript_filename,
                mime_type="text/plain",
                size_bytes=len(transcript_bytes),
                s3_key=transcript_s3_key,
                uploaded_by=requested_by,
                indexing_status=IndexingStatus.pending,
            )
            db.add(transcript_file)
            await db.flush()

            await nats_svc.publish_index_job(
                job_id=str(uuid.uuid4()),
                job_type="index",
                workspace_id=str(workspace_id),
                file_id=str(transcript_file.id),
                s3_key=transcript_s3_key,
                mime_type="text/plain",
                original_name=transcript_filename,
            )

            transcription = Transcription(
                workspace_id=workspace_id,
                created_by=requested_by,
                task_id=task_id,
                audio_file_id=audio_file_id,
                transcript_file_id=transcript_file.id,
                language=result.get("language") or task.language,
            )
            db.add(transcription)
            await db.flush()

            task.status = TranscriptionStatus.completed
            task.result = result
            task.processing_time_sec = processing_time_sec
            task.completed_at = datetime.now(UTC)
            task.transcription_id = transcription.id
            await db.commit()

    except Exception as exc:
        logger.error("transcription.results: DB/S3 error, will redeliver: %s", exc)
        await msg.nak()
        return

    await msg.ack()


async def start(js: nats.js.JetStreamContext) -> None:
    await js.subscribe(
        "transcription.results",
        durable=_DURABLE,
        cb=_handle,
    )
    logger.info("Transcription results subscriber started (durable=%s)", _DURABLE)
