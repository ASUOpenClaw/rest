from __future__ import annotations

import logging
import traceback
import uuid

from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)


class RequestIDMiddleware:
    """
    Pure ASGI middleware (no BaseHTTPMiddleware) that reads X-Request-ID from
    the incoming request or generates one, stores it in request.state, and
    echoes it back in every response header.

    Avoids the anyio task-group / event-loop conflict that BaseHTTPMiddleware
    causes in pytest-asyncio tests.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        request_id = headers.get(b"x-request-id", b"").decode() or str(uuid.uuid4())

        scope.setdefault("state", {})
        scope["state"]["request_id"] = request_id

        async def send_with_id(message: dict) -> None:
            if message["type"] == "http.response.start":
                mutable = MutableHeaders(scope=message)
                mutable.append("X-Request-ID", request_id)
            await send(message)

        await self.app(scope, receive, send_with_id)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    logger.error(
        "Unhandled exception [request_id=%s]: %s",
        request_id,
        traceback.format_exc(),
    )
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error", "request_id": request_id},
        headers={"X-Request-ID": request_id} if request_id else {},
    )
