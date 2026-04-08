"""
GoClaw admin API client.

Provisions a GoClaw tenant + API key + agent + LiteLLM provider + MCP server grant
for each workspace created in the REST API.
"""
from __future__ import annotations

import logging

import httpx

from src.core.config import settings

logger = logging.getLogger(__name__)

_SYSTEM_USER = "system"


def _admin_headers(tenant_id: str | None = None) -> dict[str, str]:
    """Headers for calls that use the master gateway token."""
    h = {
        "Authorization": f"Bearer {settings.goclaw_gateway_token}",
        "X-GoClaw-User-Id": _SYSTEM_USER,
    }
    if tenant_id:
        h["X-GoClaw-Tenant-Id"] = tenant_id
    return h


async def provision_workspace(ws_id: str, ws_name: str) -> dict:
    """
    Create a GoClaw tenant, API key, agent, LiteLLM provider, and MCP grant
    for a new workspace.

    Returns a dict with keys:
        goclaw_tenant_id  — tenant UUID
        goclaw_api_key    — tenant-bound API key (goclaw_sk_...)
        goclaw_agent_id   — agent UUID
    """
    base = settings.goclaw_gateway_url.rstrip("/")
    slug = f"ws-{ws_id}"

    async with httpx.AsyncClient(timeout=15) as client:
        # 1. Create tenant
        r = await client.post(
            f"{base}/v1/tenants",
            headers=_admin_headers(),
            json={"name": ws_name, "slug": slug},
        )
        r.raise_for_status()
        tenant_id: str = r.json()["id"]
        logger.info("GoClaw tenant created: %s for workspace %s", tenant_id, ws_id)

        # 2. Create tenant-bound API key
        r = await client.post(
            f"{base}/v1/api-keys",
            headers=_admin_headers(tenant_id=slug),
            json={"name": "shell-proxy", "scopes": ["operator.write"]},
        )
        r.raise_for_status()
        api_key: str = r.json()["key"]

        # 3. Create agent using the tenant key
        tenant_headers = {
            "Authorization": f"Bearer {api_key}",
            "X-GoClaw-User-Id": _SYSTEM_USER,
        }
        r = await client.post(
            f"{base}/v1/agents",
            headers=tenant_headers,
            json={
                "name": ws_name,
                "key": slug,
                "model": "qwen3-14b",
                "provider": "litellm",
            },
        )
        r.raise_for_status()
        agent_id: str = r.json()["id"]
        logger.info("GoClaw agent created: %s for workspace %s", agent_id, ws_id)

        # 4. Register LiteLLM provider (if configured)
        if settings.goclaw_litellm_url:
            r = await client.post(
                f"{base}/v1/providers",
                headers=tenant_headers,
                json={
                    "name": "litellm",
                    "provider_type": "openai_compat",
                    "settings": {
                        "base_url": settings.goclaw_litellm_url,
                        "api_key": settings.goclaw_litellm_api_key or "no-key",
                    },
                },
            )
            r.raise_for_status()
            logger.info("GoClaw LiteLLM provider registered for workspace %s", ws_id)

        # 5. Register MCP server and grant to agent (if configured)
        if settings.goclaw_mcp_url:
            r = await client.post(
                f"{base}/v1/mcp/servers",
                headers=tenant_headers,
                json={
                    "name": "workspace-tools",
                    "transport": "streamable-http",
                    "url": settings.goclaw_mcp_url,
                    "enabled": True,
                },
            )
            r.raise_for_status()
            mcp_server_id: str = r.json()["id"]

            r = await client.post(
                f"{base}/v1/mcp/servers/{mcp_server_id}/grants/agent",
                headers=tenant_headers,
                json={"agent_id": agent_id},
            )
            r.raise_for_status()
            logger.info("GoClaw MCP server granted to agent %s for workspace %s", agent_id, ws_id)

    return {
        "goclaw_tenant_id": tenant_id,
        "goclaw_api_key": api_key,
        "goclaw_agent_id": agent_id,
    }


async def delete_tenant(tenant_id: str) -> None:
    """Delete a GoClaw tenant and all its data."""
    base = settings.goclaw_gateway_url.rstrip("/")
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.delete(
            f"{base}/v1/tenants/{tenant_id}",
            headers=_admin_headers(),
        )
        r.raise_for_status()
        logger.info("GoClaw tenant deleted: %s", tenant_id)
