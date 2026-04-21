import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from src.models.workspace import WorkspaceRole

# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------


class WorkspaceStats(BaseModel):
    members_count: int
    files_count: int
    indexed_chunks: int


class WorkspaceOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    system_prompt: str | None
    config: dict[str, Any]
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
    stats: WorkspaceStats

    model_config = {"from_attributes": True}


class WorkspaceListItem(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    role: WorkspaceRole
    stats: WorkspaceStats
    created_at: datetime
    updated_at: datetime


class WorkspaceListOut(BaseModel):
    items: list[WorkspaceListItem]
    total: int
    page: int
    per_page: int


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(..., max_length=256)
    description: str | None = None
    system_prompt: str | None = None
    config: dict[str, Any] = {}


class WorkspacePatchRequest(BaseModel):
    name: str | None = Field(default=None, max_length=256)
    description: str | None = None
    system_prompt: str | None = None
    config: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    role: WorkspaceRole
    joined_at: datetime


class MemberListOut(BaseModel):
    items: list[MemberOut]
    total: int


class MemberAddRequest(BaseModel):
    email: str
    role: WorkspaceRole = WorkspaceRole.guest


class MemberPatchRequest(BaseModel):
    role: WorkspaceRole


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


class InviteCreateRequest(BaseModel):
    role: WorkspaceRole = WorkspaceRole.guest
    max_uses: int | None = None
    expires_in_hours: int | None = None


class InviteOut(BaseModel):
    id: uuid.UUID
    code: str
    role: WorkspaceRole
    max_uses: int | None
    used_count: int
    expires_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class JoinRequest(BaseModel):
    invite_code: str


class CronJobCreateRequest(BaseModel):
    name: str = Field(..., pattern=r"^[a-z0-9-]+$", max_length=128)
    schedule: str = Field(
        ..., description="GoClaw schedule expression: 'every 1h', '0 9 * * 1-5', etc."
    )
    message: str = Field(
        ..., description="Text the agent receives when the cron job fires"
    )


class CronJobOut(BaseModel):
    id: str
    name: str
    schedule: str = ""
    message: str = ""
    enabled: bool = True
