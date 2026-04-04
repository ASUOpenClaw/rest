import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from src.models.file import IndexingStatus

# ---------------------------------------------------------------------------
# Search request (client → REST API)
# ---------------------------------------------------------------------------


class RagSearchFilters(BaseModel):
    folder_id: uuid.UUID | None = None
    include_subfolders: bool = True
    mime_types: list[str] | None = None
    file_ids: list[uuid.UUID] | None = None


class RagSearchRequest(BaseModel):
    query: str
    top_k: int = 5
    min_score: float = 0.5
    filters: RagSearchFilters = RagSearchFilters()


# ---------------------------------------------------------------------------
# Search response (RAG service → REST API → client)
# ---------------------------------------------------------------------------


class FolderRef(BaseModel):
    id: uuid.UUID
    name: str
    path: str


class FileRef(BaseModel):
    id: str
    name: str
    folder: FolderRef | None = None


class RagChunkResult(BaseModel):
    chunk_id: str
    text: str
    score: float
    file: FileRef
    metadata: dict[str, Any] = {}


class RagSearchOut(BaseModel):
    results: list[RagChunkResult]
    total_found: int
    query_embedding_ms: int | None = None
    search_ms: int | None = None


# ---------------------------------------------------------------------------
# Status & issues (served from DB, no RAG service call)
# ---------------------------------------------------------------------------


class RagStatusOut(BaseModel):
    workspace_id: uuid.UUID
    total_files: int
    indexed_files: int
    pending_files: int
    failed_files: int
    total_chunks: int
    last_indexed_at: datetime | None


class RagIssueItem(BaseModel):
    file_id: uuid.UUID
    filename: str
    status: IndexingStatus
    error: str | None
    updated_at: datetime


class RagIssuesOut(BaseModel):
    items: list[RagIssueItem]
