"""
HTTP client for the RAG service.

REST API sends a synchronous POST /search to the RAG service and proxies
the response back to the client.  Timeout: 10 s → 503 on timeout or
service unavailability.

ACL context (excluded_file_ids) is computed here before calling out.
owner / admin always get an empty exclusion list.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx
from fastapi import HTTPException, status
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.models import File, WorkspaceMember, WorkspaceRole
from src.models.file import IndexingStatus
from src.schemas.rag import (
    RagIssueItem,
    RagIssuesOut,
    RagSearchFilters,
    RagSearchOut,
    RagStatusOut,
)
from src.services.file_permission import get_excluded_file_ids

_ROLE_ORDER = [
    WorkspaceRole.guest,
    WorkspaceRole.member,
    WorkspaceRole.admin,
    WorkspaceRole.owner,
]


def _role_gte(role: WorkspaceRole, min_role: WorkspaceRole) -> bool:
    return _ROLE_ORDER.index(role) >= _ROLE_ORDER.index(min_role)


async def search(
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    query: str,
    top_k: int,
    min_score: float,
    filters: RagSearchFilters,
    db: AsyncSession,
) -> RagSearchOut:
    # Resolve membership & role
    member = await db.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    if member is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not a workspace member"
        )

    # Build exclusion list
    if _role_gte(member.role, WorkspaceRole.admin):
        excluded_file_ids: list[str] = []
    else:
        excluded_file_ids = await get_excluded_file_ids(workspace_id, user_id, db)

    payload: dict[str, Any] = {
        "workspace_id": str(workspace_id),
        "user_id": str(user_id),
        "role": member.role.value,
        "excluded_file_ids": excluded_file_ids,
        "query": query,
        "top_k": top_k,
        "min_score": min_score,
        "filters": {
            "folder_id": str(filters.folder_id) if filters.folder_id else None,
            "include_subfolders": filters.include_subfolders,
            "mime_types": filters.mime_types,
            "file_ids": [str(fid) for fid in filters.file_ids]
            if filters.file_ids
            else None,
        },
    }

    try:
        async with httpx.AsyncClient(timeout=settings.rag_timeout_seconds) as client:
            resp = await client.post(
                f"{settings.rag_service_url}/search",
                json=payload,
            )
            resp.raise_for_status()
    except httpx.TimeoutException:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RAG service timeout",
        )
    except httpx.HTTPStatusError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"RAG service error: {exc.response.status_code}",
        )
    except httpx.RequestError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="RAG service unavailable",
        )

    return RagSearchOut.model_validate(resp.json())


# ---------------------------------------------------------------------------
# Status — served from DB, no RAG service call
# ---------------------------------------------------------------------------


async def get_status(workspace_id: uuid.UUID, db: AsyncSession) -> RagStatusOut:
    rows = await db.execute(
        select(
            File.indexing_status,
            func.count(File.id),
            func.coalesce(func.sum(File.indexed_chunks), 0),
            func.max(File.last_indexed_at),
        )
        .where(File.workspace_id == workspace_id)
        .group_by(File.indexing_status)
    )
    counts: dict[IndexingStatus, int] = {}
    chunks_total = 0
    last_indexed_at_raw = None

    for row_status, row_count, row_chunks, row_last in rows:
        counts[row_status] = row_count
        chunks_total += int(row_chunks or 0)
        if row_last and (last_indexed_at_raw is None or row_last > last_indexed_at_raw):
            last_indexed_at_raw = row_last

    total_files = sum(counts.values())
    indexed_files = counts.get(IndexingStatus.completed, 0)
    pending_files = counts.get(IndexingStatus.pending, 0) + counts.get(
        IndexingStatus.processing, 0
    )
    failed_files = counts.get(IndexingStatus.failed, 0)

    last_indexed_at = None
    if last_indexed_at_raw:
        from datetime import UTC, datetime

        try:
            last_indexed_at = datetime.fromisoformat(str(last_indexed_at_raw))
            if last_indexed_at.tzinfo is None:
                last_indexed_at = last_indexed_at.replace(tzinfo=UTC)
        except ValueError:
            pass

    return RagStatusOut(
        workspace_id=workspace_id,
        total_files=total_files,
        indexed_files=indexed_files,
        pending_files=pending_files,
        failed_files=failed_files,
        total_chunks=chunks_total,
        last_indexed_at=last_indexed_at,
    )


# ---------------------------------------------------------------------------
# Issues — failed / pending files
# ---------------------------------------------------------------------------


async def get_issues(workspace_id: uuid.UUID, db: AsyncSession) -> RagIssuesOut:
    result = await db.execute(
        select(File)
        .where(
            File.workspace_id == workspace_id,
            File.indexing_status.in_([IndexingStatus.failed, IndexingStatus.pending]),
        )
        .order_by(File.updated_at.desc())
    )
    files = result.scalars().all()
    return RagIssuesOut(
        items=[
            RagIssueItem(
                file_id=f.id,
                filename=f.original_name,
                status=f.indexing_status,
                error=f.indexing_error,
                updated_at=f.updated_at,
            )
            for f in files
        ]
    )
