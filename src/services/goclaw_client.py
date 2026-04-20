"""
GoClaw admin API client.

Provisions a GoClaw tenant + API key + agent + LiteLLM provider + MCP server grant
for each workspace created in the REST API.

Skills:
  - GOCLAW_DEFAULT_SKILLS (comma-separated names) — catalog skills granted to every agent.
  - GOCLAW_SKILLS_DIR — directory of .md files; each is packaged as a ZIP and uploaded
    to GoClaw as a custom skill (idempotent by name). Use POST /admin/skills/sync to trigger.
  - Per-workspace extras stored in workspace.config["skills_extra"] = [skill_id, ...].
"""

from __future__ import annotations

import io
import logging
import os
import zipfile

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
    slug = f"ws-{ws_id[:8]}"

    async with httpx.AsyncClient(timeout=15) as client:
        # 1. Create tenant (idempotent: reuse if slug already exists)
        r = await client.post(
            f"{base}/v1/tenants",
            headers=_admin_headers(),
            json={"name": ws_name, "slug": slug},
        )
        if r.status_code in (409, 422) or r.status_code >= 500:
            # Tenant already exists — look it up by slug
            r2 = await client.get(f"{base}/v1/tenants", headers=_admin_headers())
            r2.raise_for_status()
            raw = r2.json()
            tenants = raw if isinstance(raw, list) else raw.get("items") or raw.get("data") or raw.get("tenants") or []
            logger.info("GoClaw tenants lookup: status=%s keys=%s count=%s", r2.status_code, list(raw.keys()) if isinstance(raw, dict) else "list", len(tenants))
            existing = next((t for t in tenants if t.get("slug") == slug), None)
            if existing is None:
                logger.error("Tenant with slug %s not found in GoClaw list: %s", slug, raw)
                r.raise_for_status()  # re-raise original error
            tenant_id: str = existing["id"]
            logger.info("GoClaw tenant already exists: %s for workspace %s", tenant_id, ws_id)
        else:
            r.raise_for_status()
            tenant_id = r.json()["id"]
            logger.info("GoClaw tenant created: %s for workspace %s", tenant_id, ws_id)

        # 2. Add system user to tenant so tenant-bound keys can operate
        r = await client.post(
            f"{base}/v1/tenants/{tenant_id}/users",
            headers=_admin_headers(),
            json={"user_id": _SYSTEM_USER, "role": "admin"},
        )
        if r.status_code not in (200, 201, 409):  # 409 = already a member
            r.raise_for_status()
        logger.info("GoClaw system user added to tenant %s", tenant_id)

        # 3. Create tenant-bound API key (use tenant UUID, not slug)
        r = await client.post(
            f"{base}/v1/api-keys",
            headers=_admin_headers(tenant_id=tenant_id),
            json={"name": "shell-proxy", "scopes": ["operator.admin"]},
        )
        r.raise_for_status()
        api_key: str = r.json()["key"]

        tenant_headers = {
            "Authorization": f"Bearer {api_key}",
            "X-GoClaw-User-Id": _SYSTEM_USER,
        }

        # 4. Register LiteLLM provider FIRST (agent creation requires a valid provider)
        provider_name = "litellm"
        if settings.goclaw_litellm_url:
            r = await client.post(
                f"{base}/v1/providers",
                headers=tenant_headers,
                json={
                    "name": provider_name,
                    "provider_type": "openai_compat",
                    "enabled": True,
                    "settings": {
                        "base_url": settings.goclaw_litellm_url,
                        "api_key": settings.goclaw_litellm_api_key or "no-key",
                    },
                },
            )
            if not r.is_success:
                logger.error("GoClaw provider create failed: %s %s", r.status_code, r.text)
            r.raise_for_status()
            logger.info("GoClaw LiteLLM provider registered for workspace %s", ws_id)

        # 5. Create agent using correct field names per GoClaw HTTP API docs
        agent_body: dict = {
            "agent_key": slug,
            "display_name": ws_name,
            "provider": provider_name,
            "model": settings.goclaw_default_model,
            "agent_type": "open",
        }
        tts_cfg = _build_tts_config(
            settings.goclaw_tts_provider, settings.goclaw_tts_auto
        )
        if tts_cfg:
            agent_body["other_config"] = {"tts": tts_cfg}

        r = await client.post(
            f"{base}/v1/agents",
            headers=tenant_headers,
            json=agent_body,
        )
        if not r.is_success:
            logger.error("GoClaw agent create failed: %s %s", r.status_code, r.text)
        r.raise_for_status()
        resp = r.json()
        agent_id: str = resp["id"]
        agent_key: str = resp["agent_key"]
        logger.info("GoClaw agent created: %s (key=%s) for workspace %s", agent_id, agent_key, ws_id)

        # 6. Register embedding provider for memory search (if configured)
        if settings.goclaw_litellm_url and settings.goclaw_embedding_model:
            r = await client.post(
                f"{base}/v1/providers",
                headers=tenant_headers,
                json={
                    "name": "litellm-embeddings",
                    "provider_type": "openai_compat",
                    "settings": {
                        "base_url": settings.goclaw_litellm_url,
                        "api_key": settings.goclaw_litellm_api_key or "no-key",
                        "embedding_model": settings.goclaw_embedding_model,
                    },
                },
            )
            r.raise_for_status()
            logger.info(
                "GoClaw embedding provider registered (model: %s) for workspace %s",
                settings.goclaw_embedding_model,
                ws_id,
            )

        # 8. Register MCP server and grant to agent (if configured)
        if settings.goclaw_mcp_url:
            r = await client.post(
                f"{base}/v1/mcp/servers",
                headers=tenant_headers,
                json={
                    "name": "workspace-tools",
                    "transport": "streamable-http",
                    "url": settings.goclaw_mcp_url,
                    "tool_prefix": settings.goclaw_mcp_tool_prefix,
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
            logger.info(
                "GoClaw MCP server granted to agent %s for workspace %s",
                agent_id,
                ws_id,
            )

        # 9. Grant default skills to agent
        default_skill_ids = await _resolve_default_skill_ids(base, api_key)
        for skill_id in default_skill_ids:
            await _grant_skill_to_agent(
                client, base, tenant_headers, skill_id, agent_id
            )

    return {
        "goclaw_tenant_id": tenant_id,
        "goclaw_api_key": api_key,
        "goclaw_agent_id": agent_id,
        "goclaw_agent_key": agent_key,
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


# ---------------------------------------------------------------------------
# Skills helpers
# ---------------------------------------------------------------------------


async def _grant_skill_to_agent(
    client: httpx.AsyncClient,
    base: str,
    headers: dict[str, str],
    skill_id: str,
    agent_id: str,
) -> None:
    try:
        r = await client.post(
            f"{base}/v1/skills/{skill_id}/grants/agent",
            headers=headers,
            json={"agent_id": agent_id},
        )
        r.raise_for_status()
        logger.info("Granted skill %s to agent %s", skill_id, agent_id)
    except Exception as exc:
        logger.warning(
            "Failed to grant skill %s to agent %s: %s", skill_id, agent_id, exc
        )


async def _resolve_default_skill_ids(base: str, api_key: str) -> list[str]:
    """Resolve GOCLAW_DEFAULT_SKILLS names → IDs by listing tenant skills."""
    names = [n.strip() for n in settings.goclaw_default_skills.split(",") if n.strip()]
    if not names:
        return []
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-GoClaw-User-Id": _SYSTEM_USER,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{base}/v1/skills", headers=headers)
        if r.status_code != 200:
            logger.warning("Could not list GoClaw skills: %s", r.status_code)
            return []
        skills = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
        name_to_id = {s["name"]: s["id"] for s in skills}
        ids = []
        for name in names:
            if name in name_to_id:
                ids.append(name_to_id[name])
            else:
                logger.warning("Default skill '%s' not found in GoClaw catalog", name)
        return ids


async def list_skills() -> list[dict]:
    """List all skills visible to the gateway (admin call)."""
    base = settings.goclaw_gateway_url.rstrip("/")
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{base}/v1/skills", headers=_admin_headers())
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("items", [])


async def list_agent_skills(api_key: str, agent_id: str) -> list[dict]:
    """List skills granted to a specific agent (using tenant API key)."""
    base = settings.goclaw_gateway_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-GoClaw-User-Id": _SYSTEM_USER,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{base}/v1/agents/{agent_id}/skills", headers=headers)
        r.raise_for_status()
        data = r.json()
        return data if isinstance(data, list) else data.get("items", [])


async def grant_skill(api_key: str, agent_id: str, skill_id: str) -> None:
    """Grant a skill to a workspace's agent."""
    base = settings.goclaw_gateway_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-GoClaw-User-Id": _SYSTEM_USER,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        await _grant_skill_to_agent(client, base, headers, skill_id, agent_id)


async def revoke_skill(api_key: str, agent_id: str, skill_id: str) -> None:
    """Revoke a skill from a workspace's agent."""
    base = settings.goclaw_gateway_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-GoClaw-User-Id": _SYSTEM_USER,
    }
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.delete(
            f"{base}/v1/skills/{skill_id}/grants/agent/{agent_id}",
            headers=headers,
        )
        r.raise_for_status()
        logger.info("Revoked skill %s from agent %s", skill_id, agent_id)


async def sync_skills_from_dir() -> list[dict]:
    """
    Upload/update each .md file in GOCLAW_SKILLS_DIR as a GoClaw skill ZIP.
    Returns a list of {name, skill_id, status: "uploaded"|"already_exists"|"error"}.

    ZIP format: single SKILL.md at root, 20 MB max.
    Idempotent: if a skill with same name already exists, skip (status=already_exists).
    """
    skills_dir = settings.goclaw_skills_dir
    if not skills_dir or not os.path.isdir(skills_dir):
        return []

    base = settings.goclaw_gateway_url.rstrip("/")
    results: list[dict] = []

    # Load existing skills to avoid duplicates
    try:
        existing = await list_skills()
        existing_names = {s["name"]: s["id"] for s in existing}
    except Exception as exc:
        logger.warning("Could not list existing skills: %s", exc)
        existing_names = {}

    for fname in sorted(os.listdir(skills_dir)):
        if not fname.endswith(".md"):
            continue
        fpath = os.path.join(skills_dir, fname)
        skill_name = fname[:-3]  # strip .md

        if skill_name in existing_names:
            results.append(
                {
                    "name": skill_name,
                    "skill_id": existing_names[skill_name],
                    "status": "already_exists",
                }
            )
            continue

        try:
            with open(fpath, "rb") as f:
                md_content = f.read()

            # Package as ZIP with SKILL.md at root
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("SKILL.md", md_content)
            buf.seek(0)

            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    f"{base}/v1/skills/upload",
                    headers=_admin_headers(),
                    files={"file": (f"{skill_name}.zip", buf, "application/zip")},
                )
                r.raise_for_status()
                skill_id = r.json().get("id", "")
                results.append(
                    {"name": skill_name, "skill_id": skill_id, "status": "uploaded"}
                )
                logger.info("Uploaded skill '%s' → %s", skill_name, skill_id)
        except Exception as exc:
            logger.error("Failed to upload skill '%s': %s", skill_name, exc)
            results.append(
                {
                    "name": skill_name,
                    "skill_id": None,
                    "status": "error",
                    "error": str(exc),
                }
            )

    return results


# ---------------------------------------------------------------------------
# TTS helpers
# ---------------------------------------------------------------------------


def _build_tts_config(provider: str, auto: str) -> dict | None:
    """
    Build a GoClaw agent TTS config block.

    openai  → LiteLLM proxy → Speaches/Kokoro (mp3/wav).
              Edge TTS added as fallback: GoClaw auto-uses it when Speaches fails,
              including Telegram (which requires opus that Speaches doesn't support).
    edge    → Microsoft Edge TTS only (free, no API key, supports opus).
    off/""  → TTS disabled.
    """
    if provider == "off" or not provider:
        return None
    cfg: dict = {"provider": provider, "auto": auto, "mode": "final"}
    if provider == "openai":
        # Strip trailing /v1 — LiteLLM's openai client adds it automatically
        base_url = (settings.goclaw_litellm_url or "").rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        cfg["openai"] = {
            "api_key": settings.goclaw_litellm_api_key or "no-key",
            "model": "tts",  # matches litellm model_name
            "voice": settings.goclaw_tts_voice,
            "base_url": base_url,
        }
        # Edge TTS as fallback for Telegram (opus) and Speaches failures
        cfg["edge"] = {"enabled": True, "voice": settings.goclaw_tts_edge_voice}
    elif provider == "edge":
        cfg["edge"] = {"enabled": True, "voice": settings.goclaw_tts_edge_voice}
    return cfg


# ---------------------------------------------------------------------------
# Plugin (external MCP server) management
# ---------------------------------------------------------------------------


async def register_plugin(
    api_key: str,
    agent_id: str,
    name: str,
    url: str,
    transport: str = "streamable-http",
    tool_prefix: str | None = None,
) -> str:
    """
    Register an external MCP server as a plugin for a workspace agent.
    Returns the GoClaw MCP server ID.
    """
    base = settings.goclaw_gateway_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}", "X-GoClaw-User-Id": _SYSTEM_USER}
    body: dict = {"name": name, "transport": transport, "url": url, "enabled": True}
    if tool_prefix:
        body["tool_prefix"] = tool_prefix

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(f"{base}/v1/mcp/servers", headers=headers, json=body)
        r.raise_for_status()
        mcp_server_id: str = r.json()["id"]

        r = await client.post(
            f"{base}/v1/mcp/servers/{mcp_server_id}/grants/agent",
            headers=headers,
            json={"agent_id": agent_id},
        )
        r.raise_for_status()
        logger.info("Plugin '%s' registered and granted to agent %s", name, agent_id)
        return mcp_server_id


async def unregister_plugin(api_key: str, agent_id: str, mcp_server_id: str) -> None:
    """Revoke agent grant and delete the MCP server registration."""
    base = settings.goclaw_gateway_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}", "X-GoClaw-User-Id": _SYSTEM_USER}
    async with httpx.AsyncClient(timeout=10) as client:
        # Revoke grant first (best-effort)
        try:
            r = await client.delete(
                f"{base}/v1/mcp/servers/{mcp_server_id}/grants/agent/{agent_id}",
                headers=headers,
            )
            r.raise_for_status()
        except Exception as exc:
            logger.warning("Could not revoke plugin grant %s: %s", mcp_server_id, exc)
        # Delete the server
        r = await client.delete(
            f"{base}/v1/mcp/servers/{mcp_server_id}", headers=headers
        )
        r.raise_for_status()
        logger.info("Plugin %s unregistered", mcp_server_id)
