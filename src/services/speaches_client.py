"""
HTTP client for Speaches (ghcr.io/speaches-ai/speaches) — OpenAI-compatible
Whisper transcription server.

Endpoint: POST /v1/audio/transcriptions (multipart/form-data)

Fields:
  file             — audio/video bytes
  model            — model name (e.g. Systran/faster-whisper-large-v3)
  language         — optional ISO-639-1 code (e.g. "ru", "en")
  response_format  — "verbose_json" (segments) or "json" (text only)

Response (verbose_json):
{
  "task": "transcribe",
  "language": "russian",
  "duration": 342.5,
  "text": "...",
  "segments": [
    {"id": 0, "start": 0.0, "end": 3.2, "text": "..."},
    ...
  ]
}
"""

from __future__ import annotations

import httpx
from fastapi import HTTPException, status

from src.core.config import settings


async def transcribe(
    file_bytes: bytes,
    filename: str,
    mime_type: str,
    language: str | None,
    include_timestamps: bool,
) -> dict:
    response_format = "verbose_json" if include_timestamps else "json"

    form: dict = {"model": settings.speaches_model, "response_format": response_format}
    if language:
        form["language"] = language

    try:
        async with httpx.AsyncClient(
            timeout=settings.speaches_timeout_seconds
        ) as client:
            resp = await client.post(
                f"{settings.speaches_url}/v1/audio/transcriptions",
                data=form,
                files={"file": (filename, file_bytes, mime_type)},
            )
            resp.raise_for_status()
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Transcription service timeout",
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Transcription service error: {exc.response.status_code}",
        )
    except httpx.RequestError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Transcription service unavailable",
        )

    data = resp.json()

    # Normalise to a consistent shape regardless of response_format
    if include_timestamps:
        return {
            "text": data.get("text", ""),
            "language": data.get("language"),
            "duration_sec": data.get("duration"),
            "segments": [
                {
                    "start": seg.get("start"),
                    "end": seg.get("end"),
                    "text": seg.get("text", ""),
                }
                for seg in data.get("segments", [])
            ],
        }
    return {
        "text": data.get("text", ""),
        "language": data.get("language"),
        "duration_sec": None,
        "segments": [],
    }
