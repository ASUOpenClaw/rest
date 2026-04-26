"""
HTTP client for the Shell WS-RPC bridge service.
REST calls Shell to manage GoClaw sessions, cron jobs, and agents instead of
opening its own WebSocket connections directly.
"""

import logging

import httpx

from src.core.config import settings

logger = logging.getLogger(__name__)

_TIMEOUT = 30.0


def _headers() -> dict[str, str]:
    return {"X-Shell-Service-Key": settings.shell_service_key}


def _base() -> str:
    return settings.shell_service_url.rstrip("/")


# ---------------------------------------------------------------------------
# Cron
# ---------------------------------------------------------------------------


async def list_cron_jobs(ws_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.get(
            f"{_base()}/api/workspaces/{ws_id}/cron",
            headers=_headers(),
        )
        r.raise_for_status()
        data = r.json()
    jobs = data if isinstance(data, list) else data.get("jobs", data.get("items", []))
    return jobs


def _parse_schedule(schedule: str) -> dict:
    """Convert a human-friendly schedule string to a GoClaw schedule object.

    Accepted formats:
      "every Xh" / "every Xm" / "every Xs" / "every Xd"  → {kind: every, everyMs: ...}
      5-field cron expression (contains spaces)            → {kind: cron, expr: ...}
      ISO timestamp (digits only, 13+ chars)               → {kind: at, atMs: ...}
    """
    import re
    s = schedule.strip()

    m = re.fullmatch(r"every\s+(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)", s, re.IGNORECASE)
    if m:
        val, unit = float(m.group(1)), m.group(2).lower()
        factors = {"ms": 1, "s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000}
        return {"kind": "every", "everyMs": int(val * factors[unit])}

    if re.fullmatch(r"\d{13,}", s):
        return {"kind": "at", "atMs": int(s)}

    # Assume 5-field cron expression
    return {"kind": "cron", "expr": s}


async def create_cron_job(
    ws_id: str,
    agent_id: str,
    name: str,
    schedule: str,
    message: str,
) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.post(
            f"{_base()}/api/workspaces/{ws_id}/cron",
            headers=_headers(),
            json={
                "name": name,
                "schedule": _parse_schedule(schedule),
                "agentId": agent_id,
                "message": message,
                "lane": "cron",
            },
        )
        r.raise_for_status()
        return r.json()


async def delete_cron_job(ws_id: str, job_id: str) -> None:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.delete(
            f"{_base()}/api/workspaces/{ws_id}/cron/{job_id}",
            headers=_headers(),
        )
        r.raise_for_status()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


async def list_sessions(ws_id: str) -> list[dict]:
    """Return all GoClaw sessions for the workspace (admin view)."""
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.get(
            f"{_base()}/api/workspaces/{ws_id}/sessions",
            headers=_headers(),
        )
        r.raise_for_status()
        data = r.json()
    return data.get("sessions", [])


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


async def list_agents(ws_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.get(
            f"{_base()}/api/workspaces/{ws_id}/agents",
            headers=_headers(),
        )
        r.raise_for_status()
        data = r.json()
    agents = data if isinstance(data, list) else data.get("agents", [])
    return agents


async def update_agent(ws_id: str, agent_id: str, fields: dict) -> dict:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.patch(
            f"{_base()}/api/workspaces/{ws_id}/agents/{agent_id}",
            headers=_headers(),
            json=fields,
        )
        r.raise_for_status()
        return r.json()


async def delete_agent(ws_id: str, agent_id: str) -> None:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        r = await client.delete(
            f"{_base()}/api/workspaces/{ws_id}/agents/{agent_id}",
            headers=_headers(),
        )
        r.raise_for_status()
