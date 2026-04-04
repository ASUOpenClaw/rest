import uuid
from datetime import datetime

from pydantic import BaseModel

from src.models.file_permission import FilePermissionLevel


class FilePermissionOut(BaseModel):
    id: uuid.UUID
    file_id: uuid.UUID
    user_id: uuid.UUID
    user_email: str
    user_display_name: str
    permission: FilePermissionLevel
    granted_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime


class FilePermissionListOut(BaseModel):
    items: list[FilePermissionOut]
    total: int


class FilePermissionUpsertRequest(BaseModel):
    permission: FilePermissionLevel
