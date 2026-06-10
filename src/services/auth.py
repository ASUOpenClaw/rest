"""
Auth service: OAuth user upsert, token issuance/refresh/logout, API key CRUD.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import redis.asyncio as aioredis
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.config import settings
from src.core.security import (
    api_key_prefix,
    create_access_token,
    create_refresh_token,
    decode_token,
    generate_api_key,
    hash_api_key,
    hash_password,
    redis_refresh_key,
    verify_password,
)
from src.models import (
    ApiKey,
    OAuthAccount,
    OAuthProvider,
    User,
    WorkspaceMember,
    WorkspaceRole,
)
from src.schemas.auth import OAuthCallbackOut, TokenRefreshOut, UserOut

# ---------------------------------------------------------------------------
# Token helpers
# ---------------------------------------------------------------------------


async def issue_token_pair(
    user: User,
    redis: aioredis.Redis,
) -> dict:
    access, _ = create_access_token(user.id)
    refresh, jti = create_refresh_token(user.id)

    ttl = settings.refresh_token_expire_days * 86400
    await redis.setex(redis_refresh_key(user.id, jti), ttl, "1")

    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": settings.access_token_expire_minutes * 60,
    }


async def refresh_tokens(
    refresh_token: str,
    db: AsyncSession,
    redis: aioredis.Redis,
) -> TokenRefreshOut:
    from fastapi import HTTPException, status

    exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid refresh token"
    )

    try:
        payload = decode_token(refresh_token)
    except JWTError:
        raise exc

    if payload.get("type") != "refresh":
        raise exc

    user_id_str: str | None = payload.get("sub")
    jti: str | None = payload.get("jti")
    if not user_id_str or not jti:
        raise exc

    # Verify Redis key exists (not revoked)
    key = redis_refresh_key(user_id_str, jti)
    if not await redis.exists(key):
        raise exc

    user = await db.get(User, uuid.UUID(user_id_str))
    if user is None:
        raise exc

    # Rotate: revoke old, issue new
    await redis.delete(key)
    tokens = await issue_token_pair(user, redis)

    return TokenRefreshOut(
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        expires_in=tokens["expires_in"],
    )


async def logout(
    access_token: str,
    redis: aioredis.Redis,
) -> None:
    """Revoke the refresh token associated with this access token's user+jti."""
    try:
        payload = decode_token(access_token)
    except JWTError:
        return  # already invalid, nothing to do

    user_id = payload.get("sub")
    # We cannot revoke by access jti since refresh jti differs; revoke all refresh
    # tokens for this user by pattern scan. For simplicity we store a per-session
    # mapping: access jti → refresh jti in Redis during issuance. Instead, here
    # we accept the access token, derive the user, and delete all rt:<user_id>:* keys.
    if user_id:
        pattern = f"rt:{user_id}:*"
        cursor = 0
        while True:
            cursor, keys = await redis.scan(cursor, match=pattern, count=100)
            if keys:
                await redis.delete(*keys)
            if cursor == 0:
                break


# ---------------------------------------------------------------------------
# Email / password auth
# ---------------------------------------------------------------------------


async def register_user(
    email: str,
    password: str,
    display_name: str | None,
    db: AsyncSession,
    redis: aioredis.Redis,
) -> OAuthCallbackOut:
    from fastapi import HTTPException, status

    email = email.strip().lower()
    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Email already registered",
        )

    # Check invite stub merge
    result = await db.execute(select(User).where(User.invite_email == email))
    user = result.scalar_one_or_none()
    if user is not None:
        user.email = email
        user.display_name = display_name or email.split("@")[0]
        user.invite_email = None
        user.password_hash = hash_password(password)
    else:
        user = User(
            email=email,
            display_name=display_name or email.split("@")[0],
            password_hash=hash_password(password),
        )
        db.add(user)

    await db.commit()
    await db.refresh(user)
    tokens = await issue_token_pair(user, redis)
    return OAuthCallbackOut(user=UserOut.model_validate(user), **tokens)


async def login_user(
    email: str,
    password: str,
    db: AsyncSession,
    redis: aioredis.Redis,
) -> OAuthCallbackOut:
    from fastapi import HTTPException, status

    invalid_exc = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid email or password",
    )

    result = await db.execute(select(User).where(User.email == email.strip().lower()))
    user = result.scalar_one_or_none()
    if user is None or not user.password_hash:
        raise invalid_exc
    if not verify_password(password, user.password_hash):
        raise invalid_exc

    tokens = await issue_token_pair(user, redis)
    return OAuthCallbackOut(user=UserOut.model_validate(user), **tokens)


# ---------------------------------------------------------------------------
# OAuth — Yandex
# ---------------------------------------------------------------------------

CONNECT_STATE_PREFIX = "connect:"


def make_connect_state(user_id: str) -> str:
    return f"{CONNECT_STATE_PREFIX}{user_id}"


def parse_connect_state(state: str | None) -> str | None:
    """Return user_id if state encodes a connect request, else None."""
    if state and state.startswith(CONNECT_STATE_PREFIX):
        return state[len(CONNECT_STATE_PREFIX):]
    return None


YANDEX_AUTH_URL = "https://oauth.yandex.ru/authorize"
YANDEX_TOKEN_URL = "https://oauth.yandex.ru/token"
YANDEX_USERINFO_URL = "https://login.yandex.ru/info"


def yandex_auth_url(state: str | None = None) -> str:
    params = {
        "response_type": "code",
        "client_id": settings.yandex_client_id,
        "redirect_uri": settings.yandex_redirect_uri,
    }
    if state:
        params["state"] = state
    from urllib.parse import urlencode

    return f"{YANDEX_AUTH_URL}?{urlencode(params)}"


async def _fetch_yandex_userinfo(code: str) -> dict:
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            YANDEX_TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": settings.yandex_client_id,
                "client_secret": settings.yandex_client_secret,
                "redirect_uri": settings.yandex_redirect_uri,
            },
        )
        token_resp.raise_for_status()
        ya_access_token = token_resp.json()["access_token"]

        info_resp = await client.get(
            YANDEX_USERINFO_URL,
            headers={"Authorization": f"OAuth {ya_access_token}"},
            params={"format": "json"},
        )
        info_resp.raise_for_status()
        return info_resp.json()


async def yandex_callback(
    code: str,
    state: str | None,
    db: AsyncSession,
    redis: aioredis.Redis,
) -> OAuthCallbackOut:
    from fastapi import HTTPException, status as http_status

    info = await _fetch_yandex_userinfo(code)

    provider_user_id = str(info["id"])
    email = info.get("default_email") or info.get("emails", [None])[0] or ""
    display_name = info.get("real_name") or info.get("display_name") or email
    avatar_url: str | None = None
    if info.get("default_avatar_id"):
        avatar_url = f"https://avatars.yandex.net/get-yapic/{info['default_avatar_id']}/islands-200"

    connect_user_id = parse_connect_state(state)
    if connect_user_id:
        user = await db.get(User, uuid.UUID(connect_user_id))
        if user is None:
            raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="User not found")
        await _connect_oauth(
            db=db,
            user=user,
            provider=OAuthProvider.yandex,
            provider_user_id=provider_user_id,
            email=email,
            avatar_url=avatar_url,
        )
    else:
        user = await _upsert_oauth_user(
            db=db,
            provider=OAuthProvider.yandex,
            provider_user_id=provider_user_id,
            email=email,
            display_name=display_name,
            avatar_url=avatar_url,
        )

    tokens = await issue_token_pair(user, redis)
    return OAuthCallbackOut(user=UserOut.model_validate(user), **tokens)


# ---------------------------------------------------------------------------
# OAuth — GitHub
# ---------------------------------------------------------------------------

GITHUB_AUTH_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_USERINFO_URL = "https://api.github.com/user"
GITHUB_EMAILS_URL = "https://api.github.com/user/emails"


def github_auth_url(state: str | None = None) -> str:
    params = {
        "client_id": settings.github_client_id,
        "redirect_uri": settings.github_redirect_uri,
        "scope": "user:email",
    }
    if state:
        params["state"] = state
    from urllib.parse import urlencode

    return f"{GITHUB_AUTH_URL}?{urlencode(params)}"


async def github_callback(
    code: str,
    db: AsyncSession,
    redis: aioredis.Redis,
) -> OAuthCallbackOut:
    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            GITHUB_TOKEN_URL,
            json={
                "client_id": settings.github_client_id,
                "client_secret": settings.github_client_secret,
                "code": code,
                "redirect_uri": settings.github_redirect_uri,
            },
            headers={"Accept": "application/json"},
        )
        token_resp.raise_for_status()
        gh_access_token = token_resp.json()["access_token"]

        headers = {
            "Authorization": f"Bearer {gh_access_token}",
            "Accept": "application/vnd.github+json",
        }
        info_resp = await client.get(GITHUB_USERINFO_URL, headers=headers)
        info_resp.raise_for_status()
        info = info_resp.json()

        # GitHub may not expose email in primary response
        email: str = info.get("email") or ""
        if not email:
            emails_resp = await client.get(GITHUB_EMAILS_URL, headers=headers)
            emails_resp.raise_for_status()
            for entry in emails_resp.json():
                if entry.get("primary") and entry.get("verified"):
                    email = entry["email"]
                    break

    provider_user_id = str(info["id"])
    display_name = info.get("name") or info.get("login") or email
    avatar_url = info.get("avatar_url")

    user = await _upsert_oauth_user(
        db=db,
        provider=OAuthProvider.github,
        provider_user_id=provider_user_id,
        email=email,
        display_name=display_name,
        avatar_url=avatar_url,
    )

    tokens = await issue_token_pair(user, redis)
    return OAuthCallbackOut(user=UserOut.model_validate(user), **tokens)


# ---------------------------------------------------------------------------
# OAuth connect / disconnect
# ---------------------------------------------------------------------------


async def _connect_oauth(
    *,
    db: AsyncSession,
    user: User,
    provider: "OAuthProvider",
    provider_user_id: str,
    email: str,
    avatar_url: str | None,
) -> None:
    from fastapi import HTTPException, status as http_status

    # Check not already linked to another account
    result = await db.execute(
        select(OAuthAccount).where(
            OAuthAccount.provider == provider,
            OAuthAccount.provider_user_id == provider_user_id,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is not None and existing.user_id != user.id:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="This OAuth account is already linked to another user",
        )
    if existing is not None:
        return  # already linked to this user

    oauth_account = OAuthAccount(
        user_id=user.id,
        provider=provider,
        provider_user_id=provider_user_id,
        provider_email=email,
    )
    db.add(oauth_account)
    if not user.avatar_url and avatar_url:
        user.avatar_url = avatar_url
    await db.commit()


async def list_oauth_accounts(user: User, db: AsyncSession) -> list[OAuthAccount]:
    result = await db.execute(
        select(OAuthAccount).where(OAuthAccount.user_id == user.id)
    )
    return list(result.scalars().all())


async def disconnect_oauth(user: User, provider: str, db: AsyncSession) -> None:
    from fastapi import HTTPException, status as http_status

    try:
        provider_enum = OAuthProvider(provider)
    except ValueError:
        raise HTTPException(status_code=http_status.HTTP_400_BAD_REQUEST, detail="Unknown provider")

    result = await db.execute(
        select(OAuthAccount).where(
            OAuthAccount.user_id == user.id,
            OAuthAccount.provider == provider_enum,
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise HTTPException(status_code=http_status.HTTP_404_NOT_FOUND, detail="OAuth account not linked")

    if user.password_hash is None:
        other = await db.execute(
            select(OAuthAccount).where(
                OAuthAccount.user_id == user.id,
                OAuthAccount.provider != provider_enum,
            )
        )
        if other.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail="Cannot unlink the only auth method — set a password first",
            )

    await db.delete(account)
    await db.commit()


# ---------------------------------------------------------------------------
# Shared upsert logic
# ---------------------------------------------------------------------------


async def _upsert_oauth_user(
    *,
    db: AsyncSession,
    provider: OAuthProvider,
    provider_user_id: str,
    email: str,
    display_name: str,
    avatar_url: str | None,
) -> User:
    # Try to find existing OAuth account
    result = await db.execute(
        select(OAuthAccount).where(
            OAuthAccount.provider == provider,
            OAuthAccount.provider_user_id == provider_user_id,
        )
    )
    oauth_account = result.scalar_one_or_none()

    if oauth_account is not None:
        user = await db.get(User, oauth_account.user_id)
        # Keep avatar/display_name fresh
        user.avatar_url = avatar_url
        oauth_account.provider_email = email
        oauth_account.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(user)
        return user

    # Check for existing user by email (invite merge or prior OAuth via other provider)
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if user is None:
        # Check invite stub (invite_email match)
        result = await db.execute(select(User).where(User.invite_email == email))
        user = result.scalar_one_or_none()
        if user is not None:
            # Merge: fill real fields, clear invite_email
            user.email = email
            user.display_name = display_name
            user.avatar_url = avatar_url
            user.invite_email = None
        else:
            # Brand new user
            user = User(
                email=email,
                display_name=display_name,
                avatar_url=avatar_url,
            )
            db.add(user)
            await db.flush()
    else:
        user.avatar_url = avatar_url

    oauth_account = OAuthAccount(
        user_id=user.id,
        provider=provider,
        provider_user_id=provider_user_id,
        provider_email=email,
    )
    db.add(oauth_account)
    await db.commit()
    await db.refresh(user)
    return user


# ---------------------------------------------------------------------------
# /auth/me
# ---------------------------------------------------------------------------


async def update_me(
    user: User,
    *,
    display_name: str | None,
    avatar_url: str | None,
    new_password: str | None,
    current_password: str | None,
    db: AsyncSession,
) -> User:
    from fastapi import HTTPException, status

    if display_name is not None:
        user.display_name = display_name.strip() or user.display_name

    if avatar_url is not None:
        user.avatar_url = avatar_url or None

    if new_password is not None:
        if user.password_hash is not None:
            if not current_password or not verify_password(current_password, user.password_hash):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Current password is incorrect",
                )
        user.password_hash = hash_password(new_password)

    await db.commit()
    await db.refresh(user)
    return user


async def get_me(user: User, db: AsyncSession) -> dict:
    result = await db.execute(
        select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)
    )
    memberships = result.scalars().all()

    # Load workspace names
    from src.models import Workspace

    workspace_refs = []
    for m in memberships:
        ws = await db.get(Workspace, m.workspace_id)
        if ws:
            workspace_refs.append({"id": ws.id, "name": ws.name, "role": m.role.value})

    return {
        "id": user.id,
        "email": user.email,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "created_at": user.created_at,
        "workspaces": workspace_refs,
    }


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


async def create_api_key(
    user: User,
    name: str,
    workspace_id: uuid.UUID | None,
    scopes: list[str],
    expires_in_days: int | None,
    db: AsyncSession,
) -> tuple[ApiKey, str]:
    """Returns (ApiKey row, plaintext_key). Key shown only here."""
    raw_key = generate_api_key()
    prefix = api_key_prefix(raw_key)
    key_hash = hash_api_key(raw_key)

    expires_at = None
    if expires_in_days:
        expires_at = datetime.now(UTC) + timedelta(days=expires_in_days)

    api_key = ApiKey(
        user_id=user.id,
        workspace_id=workspace_id,
        name=name,
        key_hash=key_hash,
        key_prefix=prefix,
        scopes=scopes,
        expires_at=expires_at,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)
    return api_key, raw_key


async def list_api_keys(
    user: User,
    workspace_id: uuid.UUID | None,
    db: AsyncSession,
) -> list[ApiKey]:
    q = select(ApiKey).where(
        ApiKey.user_id == user.id,
        ApiKey.revoked_at.is_(None),
    )
    if workspace_id is not None:
        q = q.where(ApiKey.workspace_id == workspace_id)
    result = await db.execute(q)
    return list(result.scalars().all())


async def revoke_api_key(
    user: User,
    key_id: uuid.UUID,
    db: AsyncSession,
) -> None:
    from fastapi import HTTPException, status

    api_key = await db.get(ApiKey, key_id)
    if api_key is None or api_key.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="API key not found"
        )
    api_key.revoked_at = datetime.now(UTC)
    await db.commit()
