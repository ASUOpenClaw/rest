import uuid
from typing import Annotated

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.db import get_db
from src.core.deps import CurrentAuth
from src.core.redis import get_redis
from src.schemas.auth import (
    ApiKeyCreatedOut,
    ApiKeyCreateRequest,
    ApiKeyOut,
    LoginRequest,
    OAuthAccountOut,
    OAuthCallbackOut,
    RegisterRequest,
    TokenRefreshOut,
    TokenRefreshRequest,
    UpdateMeRequest,
    UserMeOut,
    UserOut,
)
from src.services import auth as auth_svc

router = APIRouter(prefix="/auth", tags=["auth"])


# ---------------------------------------------------------------------------
# Email / password
# ---------------------------------------------------------------------------


@router.post(
    "/register", response_model=OAuthCallbackOut, status_code=status.HTTP_201_CREATED
)
async def register(
    body: RegisterRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    return await auth_svc.register_user(
        email=body.email,
        password=body.password,
        display_name=body.display_name,
        db=db,
        redis=redis,
    )


@router.post("/login", response_model=OAuthCallbackOut)
async def login(
    body: LoginRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    return await auth_svc.login_user(
        email=body.email,
        password=body.password,
        db=db,
        redis=redis,
    )


@router.post("/token", response_model=OAuthCallbackOut, include_in_schema=False)
async def token_swagger(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    """OAuth2 password flow endpoint — used by Swagger UI Authorize dialog."""
    return await auth_svc.login_user(
        email=form.username,
        password=form.password,
        db=db,
        redis=redis,
    )


# ---------------------------------------------------------------------------
# OAuth — Yandex
# ---------------------------------------------------------------------------


@router.get("/yandex", summary="Initiate Yandex OAuth")
async def yandex_login(
    state: str | None = Query(default=None),
):
    url = auth_svc.yandex_auth_url(state=state)
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


@router.get("/yandex/connect", summary="Get Yandex OAuth URL to connect to existing account")
async def yandex_connect(auth: CurrentAuth):
    """Returns the Yandex OAuth URL. Client should redirect the browser to this URL."""
    state = auth_svc.make_connect_state(str(auth.user.id))
    url = auth_svc.yandex_auth_url(state=state)
    return {"url": url}


@router.get("/yandex/callback", response_model=OAuthCallbackOut)
async def yandex_callback(
    code: str = Query(...),
    state: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    try:
        return await auth_svc.yandex_callback(code=code, state=state, db=db, redis=redis)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"OAuth error: {exc}",
        )


# ---------------------------------------------------------------------------
# OAuth — GitHub
# ---------------------------------------------------------------------------


@router.get("/github", summary="Initiate GitHub OAuth")
async def github_login(
    state: str | None = Query(default=None),
):
    url = auth_svc.github_auth_url(state=state)
    return RedirectResponse(url=url, status_code=status.HTTP_302_FOUND)


@router.get("/github/callback", response_model=OAuthCallbackOut)
async def github_callback(
    code: str = Query(...),
    state: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    try:
        return await auth_svc.github_callback(code=code, db=db, redis=redis)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"OAuth error: {exc}",
        )


# ---------------------------------------------------------------------------
# Token refresh / logout
# ---------------------------------------------------------------------------


@router.post("/refresh", response_model=TokenRefreshOut)
async def refresh_token(
    body: TokenRefreshRequest,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
):
    return await auth_svc.refresh_tokens(
        refresh_token=body.refresh_token, db=db, redis=redis
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    auth: CurrentAuth,
    redis: aioredis.Redis = Depends(get_redis),
):
    # auth.user is already resolved; we need the raw token to find the jti.
    # We pass the bearer token via the request — extract from auth context's
    # resolved JWT. Since _AuthContext doesn't store the raw token, we revoke
    # all refresh tokens for the user (safe: effectively logs out all sessions).
    from src.core.security import redis_refresh_key

    pattern = f"rt:{auth.user.id}:*"
    cursor = 0
    while True:
        cursor, keys = await redis.scan(cursor, match=pattern, count=100)
        if keys:
            await redis.delete(*keys)
        if cursor == 0:
            break


# ---------------------------------------------------------------------------
# Current user
# ---------------------------------------------------------------------------


@router.get("/me", response_model=UserMeOut)
async def get_me(
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    data = await auth_svc.get_me(user=auth.user, db=db)
    return UserMeOut(**data)


@router.patch("/me", response_model=UserOut)
async def update_me(
    body: UpdateMeRequest,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    user = await auth_svc.update_me(
        user=auth.user,
        display_name=body.display_name,
        avatar_url=body.avatar_url,
        new_password=body.new_password,
        current_password=body.current_password,
        db=db,
    )
    return UserOut.model_validate(user)


@router.get("/me/oauth", response_model=list[OAuthAccountOut])
async def list_oauth_accounts(
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    accounts = await auth_svc.list_oauth_accounts(user=auth.user, db=db)
    return [OAuthAccountOut.model_validate(a) for a in accounts]


@router.delete("/me/oauth/{provider}", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_oauth(
    provider: str,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    await auth_svc.disconnect_oauth(user=auth.user, provider=provider, db=db)


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------


@router.post(
    "/api-keys", response_model=ApiKeyCreatedOut, status_code=status.HTTP_201_CREATED
)
async def create_api_key(
    body: ApiKeyCreateRequest,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    api_key, raw_key = await auth_svc.create_api_key(
        user=auth.user,
        name=body.name,
        workspace_id=body.workspace_id,
        scopes=body.scopes,
        expires_in_days=body.expires_in_days,
        db=db,
    )
    return ApiKeyCreatedOut(
        id=api_key.id,
        name=api_key.name,
        workspace_id=api_key.workspace_id,
        scopes=api_key.scopes,
        key_prefix=api_key.key_prefix,
        created_at=api_key.created_at,
        expires_at=api_key.expires_at,
        key=raw_key,
    )


@router.get("/api-keys", response_model=list[ApiKeyOut])
async def list_api_keys(
    auth: CurrentAuth,
    workspace_id: uuid.UUID | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    keys = await auth_svc.list_api_keys(
        user=auth.user,
        workspace_id=workspace_id,
        db=db,
    )
    return [ApiKeyOut.model_validate(k) for k in keys]


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
async def revoke_api_key(
    key_id: uuid.UUID,
    auth: CurrentAuth,
    db: AsyncSession = Depends(get_db),
):
    await auth_svc.revoke_api_key(user=auth.user, key_id=key_id, db=db)
