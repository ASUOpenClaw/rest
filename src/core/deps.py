"""
FastAPI dependency chain:

  get_current_user
    ├── JWT path:     decode → validate exp/iat/type → load User
    └── API key path: prefix lookup → bcrypt verify → load User + scopes

  require_workspace_member(min_role)
    └── loads WorkspaceMember, checks role ≥ min_role

  require_file_access(min_permission)
    └── checks security_mode:
          role     → maps workspace role to permission level
          per_user → loads FilePermission row (owner/admin bypass)

  require_scope(scope)   # API key requests only
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.security import decode_token, verify_api_key
from src.models import (
    ApiKey,
    File,
    FilePermission,
    FilePermissionLevel,
    FileSecurityMode,
    User,
    WorkspaceMember,
    WorkspaceRole,
)

# ---------------------------------------------------------------------------
# Scheme extractors
# ---------------------------------------------------------------------------

_bearer = HTTPBearer(auto_error=False)
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# ---------------------------------------------------------------------------
# Role ordering (higher index = more privileged)
# ---------------------------------------------------------------------------

_ROLE_ORDER = [
    WorkspaceRole.guest,
    WorkspaceRole.member,
    WorkspaceRole.admin,
    WorkspaceRole.owner,
]


def _role_gte(role: WorkspaceRole, min_role: WorkspaceRole) -> bool:
    return _ROLE_ORDER.index(role) >= _ROLE_ORDER.index(min_role)


# ---------------------------------------------------------------------------
# Permission ordering
# ---------------------------------------------------------------------------

_PERM_ORDER = [
    FilePermissionLevel.none,
    FilePermissionLevel.read,
    FilePermissionLevel.write,
]


def _perm_gte(perm: FilePermissionLevel, min_perm: FilePermissionLevel) -> bool:
    return _PERM_ORDER.index(perm) >= _PERM_ORDER.index(min_perm)


# ---------------------------------------------------------------------------
# Current user resolution
# ---------------------------------------------------------------------------


# Attached to request state by auth middlewares / deps below
class _AuthContext:
    """Carries resolved auth info through the dependency chain."""

    def __init__(self, user: User, scopes: list[str] | None = None) -> None:
        self.user = user
        self.scopes: list[str] = scopes or []


async def _resolve_jwt(
    token: str,
    db: AsyncSession,
) -> _AuthContext:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = decode_token(token)
    except JWTError:
        raise credentials_exc

    if payload.get("type") != "access":
        raise credentials_exc

    user_id_str: str | None = payload.get("sub")
    if not user_id_str:
        raise credentials_exc

    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        raise credentials_exc

    user = await db.get(User, user_id)
    if user is None:
        raise credentials_exc

    return _AuthContext(user=user)


async def _resolve_api_key(
    raw_key: str,
    db: AsyncSession,
) -> _AuthContext:
    credentials_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
    )
    prefix = (
        raw_key[7:15] if raw_key.startswith("lab_sk_") and len(raw_key) > 15 else None
    )
    if prefix is None:
        raise credentials_exc

    result = await db.execute(
        select(ApiKey).where(
            ApiKey.key_prefix == prefix,
            ApiKey.revoked_at.is_(None),
        )
    )
    api_key_rows = result.scalars().all()

    matched: ApiKey | None = None
    for row in api_key_rows:
        if verify_api_key(raw_key, row.key_hash):
            matched = row
            break

    if matched is None:
        raise credentials_exc

    from datetime import UTC, datetime

    if matched.expires_at and matched.expires_at < datetime.now(UTC):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="API key expired"
        )

    user = await db.get(User, matched.user_id)
    if user is None:
        raise credentials_exc

    return _AuthContext(user=user, scopes=list(matched.scopes or []))


async def get_current_user(
    bearer: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    api_key: Annotated[str | None, Security(_api_key_header)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> _AuthContext:
    if bearer is not None:
        return await _resolve_jwt(bearer.credentials, db)
    if api_key is not None:
        return await _resolve_api_key(api_key, db)
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


# Convenience alias used in route signatures
CurrentAuth = Annotated[_AuthContext, Depends(get_current_user)]


# ---------------------------------------------------------------------------
# Workspace membership dependency factory
# ---------------------------------------------------------------------------


def require_workspace_member(min_role: WorkspaceRole = WorkspaceRole.guest):
    """
    Returns a dependency that resolves (auth_ctx, workspace_member).
    Raises 403 if the user's role is below min_role.
    """

    async def _dep(
        workspace_id: uuid.UUID,
        auth: Annotated[_AuthContext, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> tuple[_AuthContext, WorkspaceMember]:
        result = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == auth.user.id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Not a workspace member"
            )
        if not _role_gte(member.role, min_role):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role"
            )
        return auth, member

    return _dep


# ---------------------------------------------------------------------------
# File access dependency factory
# ---------------------------------------------------------------------------


def require_file_access(min_permission: FilePermissionLevel = FilePermissionLevel.read):
    """
    Returns a dependency that resolves (auth_ctx, file, workspace_member).
    Raises 403/404 if the user cannot access the file at the requested level.
    """

    async def _dep(
        workspace_id: uuid.UUID,
        file_id: uuid.UUID,
        auth: Annotated[_AuthContext, Depends(get_current_user)],
        db: Annotated[AsyncSession, Depends(get_db)],
    ) -> tuple[_AuthContext, File, WorkspaceMember]:
        # Workspace membership check (any role)
        result = await db.execute(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == auth.user.id,
            )
        )
        member = result.scalar_one_or_none()
        if member is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Not a workspace member"
            )

        # Load file (must belong to this workspace)
        result = await db.execute(
            select(File).where(File.id == file_id, File.workspace_id == workspace_id)
        )
        file = result.scalar_one_or_none()
        if file is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
            )

        # owner / admin bypass
        if member.role in (WorkspaceRole.owner, WorkspaceRole.admin):
            return auth, file, member

        if file.security_mode == FileSecurityMode.role:
            # All roles ≥ guest get at least read; write requires member+
            effective = (
                FilePermissionLevel.write
                if _role_gte(member.role, WorkspaceRole.member)
                else FilePermissionLevel.read
            )
        else:  # per_user
            perm_result = await db.execute(
                select(FilePermission).where(
                    FilePermission.file_id == file_id,
                    FilePermission.user_id == auth.user.id,
                )
            )
            perm_row = perm_result.scalar_one_or_none()
            effective = perm_row.permission if perm_row else FilePermissionLevel.none

        if effective == FilePermissionLevel.none:
            # Treat as not found to avoid information leakage
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="File not found"
            )

        if not _perm_gte(effective, min_permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient file permission",
            )

        return auth, file, member

    return _dep


# ---------------------------------------------------------------------------
# Scope check (API key requests only)
# ---------------------------------------------------------------------------


def require_scope(scope: str):
    """Raises 403 if request came via API key and the key doesn't have the scope."""

    def _dep(auth: Annotated[_AuthContext, Depends(get_current_user)]) -> _AuthContext:
        # JWT-authenticated users are not scope-restricted
        if auth.scopes and scope not in auth.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required scope: {scope}",
            )
        return auth

    return _dep
