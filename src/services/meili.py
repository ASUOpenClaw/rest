"""
Meilisearch sync service — uses meilisearch_python_sdk AsyncClient.

Two indexes:
  workspaces — { id, name, description }
  files      — { id, workspace_id, original_name, mime_type, description, folder_path }

All write operations are fire-and-forget: errors are logged and never
propagated so Meilisearch unavailability never breaks the API.
"""

from __future__ import annotations

import logging
from typing import Any

from meilisearch_python_sdk import AsyncClient
from meilisearch_python_sdk.models.settings import MeilisearchSettings

from src.core.config import settings

logger = logging.getLogger(__name__)

INDEX_WORKSPACES = "workspaces"
INDEX_FILES = "files"

_WORKSPACE_SETTINGS = MeilisearchSettings(
    searchable_attributes=["name", "description"],
    filterable_attributes=[],
    sortable_attributes=["name"],
    displayed_attributes=["id", "name", "description"],
)

_FILE_SETTINGS = MeilisearchSettings(
    searchable_attributes=["original_name", "description"],
    filterable_attributes=["workspace_id", "mime_type", "folder_path"],
    sortable_attributes=["original_name"],
    displayed_attributes=[
        "id",
        "workspace_id",
        "original_name",
        "mime_type",
        "description",
        "folder_path",
    ],
)

_client: AsyncClient | None = None


def _get_client() -> AsyncClient:
    if _client is None:
        raise RuntimeError("Meilisearch client is not initialized")
    return _client


# ---------------------------------------------------------------------------
# Lifecycle — called from main.py lifespan
# ---------------------------------------------------------------------------


async def init(url: str, api_key: str | None = None) -> None:
    global _client
    _client = AsyncClient(url, api_key or None)
    await setup_indexes()


async def close() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


# ---------------------------------------------------------------------------
# Setup — called once at startup
# ---------------------------------------------------------------------------


async def setup_indexes() -> None:
    try:
        client = _get_client()
        result = await client.get_indexes()
        existing = {idx.uid for idx in result} if result else set()
        for uid, index_settings in [
            (INDEX_WORKSPACES, _WORKSPACE_SETTINGS),
            (INDEX_FILES, _FILE_SETTINGS),
        ]:
            if uid not in existing:
                await client.create_index(uid, primary_key="id")
            await client.index(uid).update_settings(index_settings)
        logger.info("Meilisearch indexes ready")
    except Exception as exc:
        logger.warning("Meilisearch setup failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Workspace sync
# ---------------------------------------------------------------------------


async def index_workspace(
    workspace_id: str, name: str, description: str | None
) -> None:
    doc = {"id": workspace_id, "name": name, "description": description or ""}
    await _upsert(INDEX_WORKSPACES, [doc])


async def delete_workspace(workspace_id: str) -> None:
    await _delete(INDEX_WORKSPACES, workspace_id)


# ---------------------------------------------------------------------------
# File sync
# ---------------------------------------------------------------------------


async def index_file(
    file_id: str,
    workspace_id: str,
    original_name: str,
    mime_type: str,
    description: str | None,
    folder_path: str | None,
) -> None:
    doc = {
        "id": file_id,
        "workspace_id": workspace_id,
        "original_name": original_name,
        "mime_type": mime_type,
        "description": description or "",
        "folder_path": folder_path or "",
    }
    await _upsert(INDEX_FILES, [doc])


async def delete_file(file_id: str) -> None:
    await _delete(INDEX_FILES, file_id)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _upsert(index_name: str, docs: list[dict[str, Any]]) -> None:
    try:
        await _get_client().index(index_name).add_documents(docs)
    except Exception as exc:
        logger.warning("Meilisearch upsert failed (index=%s): %s", index_name, exc)


async def _delete(index_name: str, doc_id: str) -> None:
    try:
        await _get_client().index(index_name).delete_document(doc_id)
    except Exception as exc:
        logger.warning(
            "Meilisearch delete failed (index=%s, id=%s): %s", index_name, doc_id, exc
        )
