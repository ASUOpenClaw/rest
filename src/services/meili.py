"""
Meilisearch sync service — uses the official meilisearch SDK (synchronous),
wrapped in asyncio.to_thread so the event loop is never blocked.

Two indexes:
  workspaces — { id, name, description }
  files      — { id, workspace_id, original_name, mime_type, description, folder_path }

All write operations are fire-and-forget: errors are logged and never
propagated so Meilisearch unavailability never breaks the API.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import meilisearch

from src.core.config import settings

logger = logging.getLogger(__name__)

INDEX_WORKSPACES = "workspaces"
INDEX_FILES = "files"

_WORKSPACE_SETTINGS = {
    "searchableAttributes": ["name", "description"],
    "filterableAttributes": [],
    "sortableAttributes": ["name"],
    "displayedAttributes": ["id", "name", "description"],
}

_FILE_SETTINGS = {
    "searchableAttributes": ["original_name", "description"],
    "filterableAttributes": ["workspace_id", "mime_type", "folder_path"],
    "sortableAttributes": ["original_name"],
    "displayedAttributes": [
        "id",
        "workspace_id",
        "original_name",
        "mime_type",
        "description",
        "folder_path",
    ],
}


def _client() -> meilisearch.Client:
    return meilisearch.Client(
        settings.meilisearch_url,
        settings.meilisearch_api_key or None,
    )


# ---------------------------------------------------------------------------
# Setup — called once at startup
# ---------------------------------------------------------------------------


def _setup_indexes_sync() -> None:
    client = _client()
    existing = {idx.uid for idx in client.get_indexes()["results"]}
    for uid, index_settings in [
        (INDEX_WORKSPACES, _WORKSPACE_SETTINGS),
        (INDEX_FILES, _FILE_SETTINGS),
    ]:
        if uid not in existing:
            client.create_index(uid, {"primaryKey": "id"})
        client.index(uid).update_settings(index_settings)


async def setup_indexes() -> None:
    try:
        await asyncio.to_thread(_setup_indexes_sync)
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
        await asyncio.to_thread(lambda: _client().index(index_name).add_documents(docs))
    except Exception as exc:
        logger.warning("Meilisearch upsert failed (index=%s): %s", index_name, exc)


async def _delete(index_name: str, doc_id: str) -> None:
    try:
        await asyncio.to_thread(
            lambda: _client().index(index_name).delete_document(doc_id)
        )
    except Exception as exc:
        logger.warning(
            "Meilisearch delete failed (index=%s, id=%s): %s", index_name, doc_id, exc
        )
