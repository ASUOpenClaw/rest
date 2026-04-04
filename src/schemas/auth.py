import uuid
from datetime import datetime

from pydantic import BaseModel

# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------


class UserWorkspaceRef(BaseModel):
    id: uuid.UUID
    name: str
    role: str


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    display_name: str
    avatar_url: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class UserMeOut(UserOut):
    workspaces: list[UserWorkspaceRef] = []


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds


class TokenRefreshRequest(BaseModel):
    refresh_token: str


class TokenRefreshOut(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int


class OAuthCallbackOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


class ApiKeyCreateRequest(BaseModel):
    name: str
    workspace_id: uuid.UUID | None = None
    scopes: list[str] = []
    expires_in_days: int | None = None


class ApiKeyOut(BaseModel):
    id: uuid.UUID
    name: str
    workspace_id: uuid.UUID | None
    scopes: list[str]
    key_prefix: str
    created_at: datetime
    expires_at: datetime | None

    model_config = {"from_attributes": True}


class ApiKeyCreatedOut(ApiKeyOut):
    key: str  # shown only once
