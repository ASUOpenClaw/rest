import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from src.models.file import FileSecurityMode, IndexingStatus


class FolderRef(BaseModel):
    id: uuid.UUID
    name: str
    path: str


class UploaderRef(BaseModel):
    id: uuid.UUID
    display_name: str


class FileOut(BaseModel):
    id: uuid.UUID
    original_name: str
    mime_type: str
    size_bytes: int
    folder: FolderRef | None
    description: str | None
    s3_key: str
    uploaded_by: UploaderRef | None
    security_mode: FileSecurityMode
    indexing_status: IndexingStatus
    indexed_chunks: int
    file_metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class FileListOut(BaseModel):
    items: list[FileOut]
    total: int
    page: int
    per_page: int


class FilePatchRequest(BaseModel):
    description: str | None = None
    folder_id: uuid.UUID | None = None
    move_to_root: bool = False  # explicit null move
    security_mode: FileSecurityMode | None = None


class DownloadUrlOut(BaseModel):
    url: str
    expires_in: int
    filename: str
    content_type: str


class ReindexOut(BaseModel):
    file_id: uuid.UUID
    indexing_status: IndexingStatus
    message: str


class PublishWorkspaceFileRequest(BaseModel):
    goclaw_path: str
    dest_filename: str
    folder_id: uuid.UUID | None = None
    description: str | None = None
    auto_delete: bool = True
