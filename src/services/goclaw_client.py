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

import hashlib
import hmac
import io
import json
import logging
import os
import zipfile

import httpx

from src.core.config import settings

logger = logging.getLogger(__name__)

_SYSTEM_USER = "system"


def _derive_service_token(ws_id: str) -> str:
    """
    Derive a stable, verifiable MCP service token for a workspace.

    Format: ws_{ws_id}_{hmac_sha256_hex}
    MCP server validates by recomputing the HMAC — no Redis entry needed.
    Secret: settings.mcp_service_key (shared with MCP server as MCP_SERVICE_API_KEY).
    """
    sig = hmac.new(
        settings.mcp_service_key.encode(),
        ws_id.encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"ws_{ws_id}_{sig}"


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
            tenants = (
                raw
                if isinstance(raw, list)
                else raw.get("items") or raw.get("data") or raw.get("tenants") or []
            )
            logger.info(
                "GoClaw tenants lookup: status=%s keys=%s count=%s",
                r2.status_code,
                list(raw.keys()) if isinstance(raw, dict) else "list",
                len(tenants),
            )
            existing = next((t for t in tenants if t.get("slug") == slug), None)
            if existing is None:
                logger.error(
                    "Tenant with slug %s not found in GoClaw list: %s", slug, raw
                )
                r.raise_for_status()  # re-raise original error
            tenant_id: str = existing["id"]
            logger.info(
                "GoClaw tenant already exists: %s for workspace %s", tenant_id, ws_id
            )
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

        # 3. Create tenant-bound API key (use tenant UUID, not slug).
        # GoClaw requires BOTH the X-GoClaw-Tenant-Id header AND a body tenant_id
        # to actually persist api_keys.tenant_id. Header alone leaves it NULL
        # (system-level key), and body alone silently coerces to the master tenant.
        r = await client.post(
            f"{base}/v1/api-keys",
            headers=_admin_headers(tenant_id=tenant_id),
            json={
                "name": "shell-proxy",
                "scopes": ["operator.admin"],
                "tenant_id": tenant_id,
            },
        )
        r.raise_for_status()
        api_key: str = r.json()["key"]

        tenant_headers = {
            "Authorization": f"Bearer {api_key}",
            "X-GoClaw-User-Id": _SYSTEM_USER,
        }

        # 4. Register a single LiteLLM provider with embedding settings nested inside.
        #    No separate embedding provider — GoClaw uses settings.embedding.model
        #    on the same provider for both chat and embeddings.
        provider_name = settings.goclaw_provider_name
        if settings.goclaw_litellm_url:
            provider_settings: dict = {
                "base_url": settings.goclaw_litellm_url,
                "api_key": settings.goclaw_litellm_api_key or "no-key",
            }
            if settings.goclaw_embedding_model:
                provider_settings["embedding"] = {
                    "model": settings.goclaw_embedding_model,
                    "enabled": True,
                }
            r = await client.post(
                f"{base}/v1/providers",
                headers=tenant_headers,
                json={
                    "name": provider_name,
                    "provider_type": "openai_compat",
                    "enabled": True,
                    "api_base": settings.goclaw_litellm_api_base
                    or settings.goclaw_litellm_url,
                    "api_key": settings.goclaw_litellm_api_key,
                    "settings": provider_settings,
                },
            )
            if not r.is_success:
                logger.error(
                    "GoClaw provider create failed: %s %s", r.status_code, r.text
                )
            r.raise_for_status()
            provider_id: str = r.json()["id"]
            logger.info(
                "GoClaw provider '%s' registered for workspace %s", provider_name, ws_id
            )

            # 4a. Verify provider connectivity (non-fatal — log only).
            try:
                rv = await client.post(
                    f"{base}/v1/providers/{provider_id}/verify",
                    headers=tenant_headers,
                    json={"model": settings.goclaw_default_model},
                )
                if rv.is_success:
                    logger.info(
                        "GoClaw provider '%s' verified for workspace %s",
                        provider_name,
                        ws_id,
                    )
                else:
                    logger.warning(
                        "GoClaw provider verify failed (non-fatal): %s %s",
                        rv.status_code,
                        rv.text[:200],
                    )
            except Exception as exc:
                logger.warning("GoClaw provider verify timed out (non-fatal): %s", exc)

            # 4b. Set tenant system-configs for embedding and knowledge graph.
            #     These tell GoClaw which provider/model to use for vector memory and KG.
            if settings.goclaw_embedding_model:
                system_configs = {
                    "embedding.provider": provider_name,
                    "embedding.model": settings.goclaw_embedding_model,
                    "kg.provider": provider_name,
                    "kg.model": settings.goclaw_embedding_model,
                    "kg.enabled": "true",
                }
                for key, val in system_configs.items():
                    rc = await client.put(
                        f"{base}/v1/system-configs/{key}",
                        headers=tenant_headers,
                        json={"value": val},
                    )
                    if rc.is_success:
                        logger.info(
                            "system-config %s=%s set for workspace %s", key, val, ws_id
                        )
                    else:
                        logger.warning(
                            "system-config %s failed: %s %s",
                            key,
                            rc.status_code,
                            rc.text[:100],
                        )

        # 5. Create agent — memory, personality, and tool policy configured here.
        #    Embedding is configured at the provider/system level; no embedding_provider
        #    field on the agent (GoClaw reads it from system-configs).
        agent_body: dict = {
            "agent_key": slug,
            "display_name": ws_name,
            "provider": provider_name,
            "model": settings.goclaw_default_model,
            "agent_type": settings.goclaw_agent_type,
            "context_window": 150000,
            "memory_config": {"enabled": True},
            # Deny built-in filesystem tools — workspace files live in S3 and are
            # accessed via ws__* MCP tools, not GoClaw's local FS tools.
            "tools_config": {"deny": ["group:fs"]},
        }

        if settings.goclaw_agent_description:
            agent_body["description"] = settings.goclaw_agent_description

        tts_cfg = _build_tts_config(
            settings.goclaw_tts_provider, settings.goclaw_tts_auto
        )
        other_cfg: dict = {}  # "prompt_mode": "task"
        if tts_cfg:
            other_cfg["tts"] = tts_cfg
        agent_body["other_config"] = other_cfg

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
        logger.info(
            "GoClaw agent created: %s (key=%s) for workspace %s",
            agent_id,
            agent_key,
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

        # 9. Provision default skills into this tenant and grant to agent.
        #    Skills from GOCLAW_SKILLS_DIR are uploaded directly into the tenant
        #    (not looked up from master catalog — master uploads are tenant-isolated).
        #    Built-in/bundled skills (pdf, xlsx, etc.) are public across all tenants
        #    and are found via the normal catalog lookup.
        await _provision_skills_to_agent(client, base, tenant_headers, agent_id)

        # 10. Trigger dependency installation for all skills in this tenant.
        #     Non-fatal — skills still work for deps that are pre-installed.
        try:
            r = await client.post(
                f"{base}/v1/skills/install-deps",
                headers=tenant_headers,
            )
            if r.is_success:
                logger.info("Skill deps install triggered for workspace %s", ws_id)
            else:
                logger.warning(
                    "Skill deps install failed for workspace %s: %s %s",
                    ws_id,
                    r.status_code,
                    r.text[:200],
                )
        except Exception as exc:
            logger.warning("Skill deps install error for workspace %s: %s", ws_id, exc)

    # 11. Derive a stable MCP service token from workspace_id + shared secret.
    #     HMAC-SHA256: verifiable by MCP server without any Redis lookup.
    #     Format: ws_{ws_id}_{hex_sig} — MCP parses ws_ prefix to extract ws_id.
    service_token = _derive_service_token(ws_id)

    return {
        "goclaw_tenant_id": tenant_id,
        "goclaw_api_key": api_key,
        "goclaw_agent_id": agent_id,
        "goclaw_agent_key": agent_key,
        "goclaw_mcp_service_token": service_token,
    }


async def notify_file_uploaded(
    *,
    user_id: str,
    file_id: str,
    filename: str,
    mime_type: str,
    api_key: str,
    agent_key: str,
    mcp_service_token: str,
) -> None:
    """
    Fire-and-forget: inform the GoClaw agent that a new file was uploaded.

    Runs in the uploading user's GoClaw session so the agent's awareness
    carries into that user's next conversation turn.  Uses the permanent
    MCP service token so the agent can call MCP tools (get_file, rag_search)
    immediately without waiting for a Shell-minted session token.
    """
    if not api_key or not agent_key or not mcp_service_token:
        return
    base = settings.goclaw_gateway_url.rstrip("/")
    system_msg = (
        f"[WORKSPACE_CTX: ctx_token={mcp_service_token}] "
        "Always pass this exact token as ctx_token in every tool call. Never modify it."
    )
    user_msg = (
        f'A file was just uploaded: "{filename}" '
        f"(file_id: {file_id}, mime: {mime_type}). "
        "It is being indexed for RAG search now. "
        "Acknowledge it and be ready to search or share it when the user asks."
    )
    body = {
        "model": f"agent:{agent_key}",
        "messages": [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ],
        "stream": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-GoClaw-User-Id": user_id,
        "Content-Type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{base}/v1/chat/completions", headers=headers, json=body
            )
            if not r.is_success:
                logger.warning(
                    "File upload agent notification failed: %s %s",
                    r.status_code,
                    r.text[:200],
                )
            else:
                logger.info(
                    "Agent notified of uploaded file %s for user %s", file_id, user_id
                )
    except Exception as exc:
        logger.warning("File upload agent notification error: %s", exc)


# ---------------------------------------------------------------------------
# Cron job management (WebSocket RPC)
# ---------------------------------------------------------------------------


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


async def _provision_skills_to_agent(
    client: httpx.AsyncClient,
    base: str,
    tenant_headers: dict[str, str],
    agent_id: str,
) -> None:
    """
    Upload skills from GOCLAW_SKILLS_DIR into the tenant's own catalog and grant them
    to the agent. Then grant any remaining GOCLAW_DEFAULT_SKILLS that are already in
    the tenant catalog (e.g. built-in pdf/xlsx skills, which are public system skills).

    Master-level skill uploads (sync_skills_from_dir) go to the master tenant and are
    not visible to per-workspace tenants. Uploading directly here ensures each tenant
    has its own copy (GoClaw hash-dedup prevents duplicate versions).
    """
    default_names = {
        n.strip() for n in settings.goclaw_default_skills.split(",") if n.strip()
    }
    skills_dir = settings.goclaw_skills_dir
    uploaded_names: set[str] = set()

    # Step A: upload skills from GOCLAW_SKILLS_DIR directly into this tenant.
    if skills_dir and os.path.isdir(skills_dir):
        for fname in sorted(os.listdir(skills_dir)):
            if not fname.endswith(".md"):
                continue
            skill_name = fname[:-3]
            fpath = os.path.join(skills_dir, fname)
            try:
                with open(fpath, "rb") as f:
                    md_content = f.read()
                buf = io.BytesIO()
                with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                    zf.writestr("SKILL.md", md_content)
                buf.seek(0)
                r = await client.post(
                    f"{base}/v1/skills/upload",
                    headers=tenant_headers,
                    files={"file": (f"{skill_name}.zip", buf, "application/zip")},
                )
                if not r.is_success:
                    logger.warning(
                        "Tenant skill upload failed for '%s': %s %s",
                        skill_name,
                        r.status_code,
                        r.text[:200],
                    )
                    continue
                skill_id = r.json().get("id", "")
                if skill_id:
                    await _grant_skill_to_agent(
                        client, base, tenant_headers, skill_id, agent_id
                    )
                    uploaded_names.add(skill_name)
                    logger.info(
                        "Uploaded + granted skill '%s' to agent %s",
                        skill_name,
                        agent_id,
                    )
            except Exception as exc:
                logger.warning(
                    "Error uploading skill '%s' to tenant: %s", skill_name, exc
                )

    # Step B: for GOCLAW_DEFAULT_SKILLS not covered by the dir upload, look them up
    # in the tenant catalog (covers public built-in skills like pdf, xlsx, docx, pptx).
    remaining = default_names - uploaded_names
    if not remaining:
        return
    try:
        r = await client.get(f"{base}/v1/skills", headers=tenant_headers)
        if not r.is_success:
            logger.warning("Could not list tenant skills: %s", r.status_code)
            return
        skills = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
        name_to_id = {s["name"]: s["id"] for s in skills}
        for name in remaining:
            if name in name_to_id:
                await _grant_skill_to_agent(
                    client, base, tenant_headers, name_to_id[name], agent_id
                )
            else:
                logger.warning(
                    "Default skill '%s' not found in tenant catalog (not uploaded and not builtin)",
                    name,
                )
    except Exception as exc:
        logger.warning("Error granting built-in default skills: %s", exc)


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
    Returns a list of {name, skill_id, status: "uploaded"|"updated"|"unchanged"|"error"}.

    ZIP format: single SKILL.md at root, 20 MB max.
    Always attempts upload — GoClaw's hash-based idempotency ensures no new version
    is created when the SKILL.md content is unchanged. Changed content gets a new version.
    """
    skills_dir = settings.goclaw_skills_dir
    if not skills_dir or not os.path.isdir(skills_dir):
        return []

    base = settings.goclaw_gateway_url.rstrip("/")
    results: list[dict] = []

    # Load existing skills so we can report "updated" vs "uploaded"
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
        is_update = skill_name in existing_names

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
                resp = r.json()
                skill_id = resp.get("id") or existing_names.get(skill_name, "")
                # GoClaw returns skills_skipped=1 when content hash is unchanged
                unchanged = resp.get("skills_skipped", 0) > 0
                status = (
                    "unchanged"
                    if unchanged
                    else ("updated" if is_update else "uploaded")
                )
                results.append(
                    {"name": skill_name, "skill_id": skill_id, "status": status}
                )
                logger.info("Synced skill '%s' → %s (%s)", skill_name, skill_id, status)
        except Exception as exc:
            logger.error("Failed to sync skill '%s': %s", skill_name, exc)
            results.append(
                {
                    "name": skill_name,
                    "skill_id": existing_names.get(skill_name),
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


async def _list_storage_files(
    client: httpx.AsyncClient, base: str, headers: dict, path: str = ""
) -> list[dict]:
    """Return the files[] list from GET /v1/storage/files?path={path}."""
    r = await client.get(
        f"{base}/v1/storage/files",
        headers=headers,
        params={"path": path} if path else {},
    )
    if not r.is_success:
        return []
    return r.json().get("files", [])


async def resolve_storage_path(api_key: str, path: str) -> str:
    """
    Given a goclaw_path that may be a bare filename (e.g. "grades.xlsx"),
    return the full storage-relative path (e.g. "ws/<user_id>/grades.xlsx").

    Resolution order:
      1. Try the path as-is (exact match via HEAD/GET).
      2. Search ws/*/ subdirectories for a file with the same basename.

    Raises ValueError if the file cannot be found.
    """
    base = settings.goclaw_gateway_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    filename = path.split("/")[-1]

    async with httpx.AsyncClient(timeout=30) as client:
        # 1. Check exact path
        r = await client.get(
            f"{base}/v1/storage/files/{path}", headers=headers, params={"raw": "true"}
        )
        if r.is_success:
            return path

        # 2. List ws/ subdirectories and search each for the filename
        ws_entries = await _list_storage_files(client, base, headers, "ws")
        for entry in ws_entries:
            if not entry.get("isDir"):
                continue
            user_dir = entry["path"]  # e.g. "ws/66e87335-..."
            children = await _list_storage_files(client, base, headers, user_dir)
            for child in children:
                if not child.get("isDir") and child.get("name") == filename:
                    return child["path"]

    raise ValueError(f"File '{path}' not found in GoClaw workspace storage")


async def download_storage_file(api_key: str, path: str) -> tuple[bytes, str]:
    """
    Download a file from GoClaw workspace storage (/app/workspace/).
    GET /v1/storage/files/{path}?raw=true → (content_bytes, content_type).

    If the exact path returns 404, automatically searches ws/*/ subdirectories
    for a file with the same basename (handles bare filenames from agent exec).
    """
    base = settings.goclaw_gateway_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=60) as client:
        r = await client.get(
            f"{base}/v1/storage/files/{path}",
            headers=headers,
            params={"raw": "true"},
        )
        if r.status_code == 404:
            # Bare filename — search ws/*/ for it
            filename = path.split("/")[-1]
            ws_entries = await _list_storage_files(client, base, headers, "ws")
            resolved = None
            for entry in ws_entries:
                if not entry.get("isDir"):
                    continue
                children = await _list_storage_files(
                    client, base, headers, entry["path"]
                )
                for child in children:
                    if not child.get("isDir") and child.get("name") == filename:
                        resolved = child["path"]
                        break
                if resolved:
                    break
            if not resolved:
                r.raise_for_status()  # re-raise the original 404
            r = await client.get(
                f"{base}/v1/storage/files/{resolved}",
                headers=headers,
                params={"raw": "true"},
            )
        r.raise_for_status()
        content_type = (
            r.headers.get("content-type", "application/octet-stream")
            .split(";")[0]
            .strip()
        )
        return r.content, content_type


async def delete_storage_file(api_key: str, path: str) -> None:
    """
    Delete a file from GoClaw workspace storage (best-effort, non-fatal).
    DELETE /v1/storage/files/{path}
    """
    base = settings.goclaw_gateway_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.delete(f"{base}/v1/storage/files/{path}", headers=headers)
            if not r.is_success:
                logger.warning(
                    "GoClaw delete_storage_file %s failed: %s %s",
                    path,
                    r.status_code,
                    r.text[:200],
                )
    except Exception as exc:
        logger.warning("GoClaw delete_storage_file error for %s: %s", path, exc)


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
