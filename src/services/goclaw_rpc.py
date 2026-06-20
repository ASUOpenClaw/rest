"""
GoClaw WS-RPC connection pool.

Replaces shell_client. REST talks to GoClaw directly via the WebSocket RPC
protocol instead of proxying through the Shell service.

Protocol: {"type":"req","id":"<uuid>","method":"<method>","params":{...}}
Response: {"type":"res","id":"<uuid>","result":{...},"error":null}

One WS connection per (api_key). Connections are created lazily and cleaned up
after 30 minutes of idle. On disconnect the next call reconnects automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid as _uuid_mod
from datetime import UTC, datetime

import websockets
import websockets.asyncio.client

from src.core.config import settings

logger = logging.getLogger(__name__)

_IDLE_TIMEOUT_S = 1800  # 30 min
_CONNECT_TIMEOUT_S = 10.0
_CALL_TIMEOUT_S = 30.0


class _Conn:
    def __init__(self, api_key: str, agent_key: str) -> None:
        self._api_key = api_key
        self._agent_key = agent_key
        self._ws: websockets.asyncio.client.ClientConnection | None = None
        self._pending: dict[str, asyncio.Future] = {}
        self._reader: asyncio.Task | None = None
        self._last_used = datetime.now(UTC)
        self.alive = False

    async def connect(self) -> None:
        ws_url = settings.goclaw_gateway_ws_url
        self._ws = await websockets.asyncio.client.connect(
            ws_url, open_timeout=_CONNECT_TIMEOUT_S
        )
        loop = asyncio.get_event_loop()
        conn_fut: asyncio.Future = loop.create_future()
        self._pending["connect"] = conn_fut
        await self._ws.send(
            json.dumps(
                {
                    "type": "req",
                    "id": "connect",
                    "method": "connect",
                    "params": {
                        "apiKey": self._api_key,
                        "agentId": self._agent_key,
                        "sessionKey": "system",
                        "userId": "system",
                    },
                }
            )
        )
        self._reader = asyncio.create_task(self._read_loop(), name="goclaw-rpc-reader")
        await asyncio.wait_for(conn_fut, timeout=_CONNECT_TIMEOUT_S)
        self.alive = True
        logger.debug("goclaw_rpc: connected for agent %s", self._agent_key)

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                if msg.get("type") != "res":
                    continue
                msg_id = str(msg.get("id", ""))
                fut = self._pending.pop(msg_id, None)
                if fut is None or fut.done():
                    continue
                if msg.get("error"):
                    fut.set_exception(
                        RuntimeError(msg["error"].get("message", "goclaw rpc error"))
                    )
                else:
                    fut.set_result(msg.get("result"))
        except Exception as exc:
            logger.debug("goclaw_rpc: read loop ended: %s", exc)
        finally:
            self.alive = False
            for fut in list(self._pending.values()):
                if not fut.done():
                    fut.set_exception(RuntimeError("goclaw_rpc: connection closed"))
            self._pending.clear()

    async def call(self, method: str, params: dict) -> dict:
        self._last_used = datetime.now(UTC)
        call_id = str(_uuid_mod.uuid4())
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[call_id] = fut
        await self._ws.send(
            json.dumps(
                {"type": "req", "id": call_id, "method": method, "params": params}
            )
        )
        result = await asyncio.wait_for(fut, timeout=_CALL_TIMEOUT_S)
        return result or {}

    @property
    def idle_seconds(self) -> float:
        return (datetime.now(UTC) - self._last_used).total_seconds()

    async def close(self) -> None:
        self.alive = False
        if self._reader:
            self._reader.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass


class GoclawRpcPool:
    def __init__(self) -> None:
        self._conns: dict[str, _Conn] = {}  # api_key → conn
        self._lock = asyncio.Lock()
        self._cleaner: asyncio.Task | None = None

    def start(self) -> None:
        self._cleaner = asyncio.create_task(
            self._cleanup_loop(), name="goclaw-rpc-cleanup"
        )

    async def stop(self) -> None:
        if self._cleaner:
            self._cleaner.cancel()
        for conn in list(self._conns.values()):
            await conn.close()
        self._conns.clear()

    async def _get_conn(self, api_key: str, agent_key: str) -> _Conn:
        async with self._lock:
            conn = self._conns.get(api_key)
            if conn is None or not conn.alive:
                conn = _Conn(api_key, agent_key)
                try:
                    await conn.connect()
                except Exception as exc:
                    raise RuntimeError(f"goclaw_rpc: connect failed: {exc}") from exc
                self._conns[api_key] = conn
        return conn

    async def call(
        self, api_key: str, agent_key: str, method: str, params: dict
    ) -> dict:
        conn = await self._get_conn(api_key, agent_key)
        try:
            return await conn.call(method, params)
        except Exception:
            # One retry with a fresh connection.
            async with self._lock:
                if self._conns.get(api_key) is conn:
                    del self._conns[api_key]
            conn = await self._get_conn(api_key, agent_key)
            return await conn.call(method, params)

    async def _cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            async with self._lock:
                stale = [
                    k
                    for k, c in self._conns.items()
                    if c.idle_seconds > _IDLE_TIMEOUT_S or not c.alive
                ]
                for k in stale:
                    c = self._conns.pop(k)
                    asyncio.create_task(c.close())

    # ── Convenience methods ──────────────────────────────────────────────────

    async def list_sessions(self, api_key: str, agent_key: str) -> list[dict]:
        result = await self.call(api_key, agent_key, "sessions.list", {})
        return result.get("sessions", []) if isinstance(result, dict) else []

    async def set_agent_file(
        self, api_key: str, agent_key: str, file_name: str, content: str
    ) -> None:
        await self.call(
            api_key,
            agent_key,
            "agents.files.set",
            {
                "agentId": agent_key,
                "filename": file_name,
                "content": content,
            },
        )

    async def list_cron_jobs(self, api_key: str, agent_key: str) -> list[dict]:
        result = await self.call(
            api_key, agent_key, "cron.list", {"agentId": agent_key}
        )
        return result.get("jobs", []) if isinstance(result, dict) else []

    async def create_cron_job(
        self,
        api_key: str,
        agent_key: str,
        name: str,
        schedule: dict,
        message: str,
    ) -> dict:
        return await self.call(
            api_key,
            agent_key,
            "cron.create",
            {
                "agentId": agent_key,
                "name": name,
                "schedule": schedule,
                "message": message,
                "lane": "cron",
            },
        )

    async def delete_cron_job(self, api_key: str, agent_key: str, job_id: str) -> None:
        await self.call(api_key, agent_key, "cron.delete", {"id": job_id})


# Module-level singleton — started/stopped in FastAPI lifespan.
_pool: GoclawRpcPool | None = None


def get_pool() -> GoclawRpcPool:
    if _pool is None:
        raise RuntimeError("goclaw_rpc pool not started — call init_pool() in lifespan")
    return _pool


def init_pool() -> GoclawRpcPool:
    global _pool
    _pool = GoclawRpcPool()
    return _pool
