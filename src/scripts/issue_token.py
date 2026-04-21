#!/usr/bin/env python
"""
Issue an access + refresh token pair for an existing user.

Usage (from rest/ directory):
    uv run python scripts/issue_token.py --username admin@example.com
"""

import argparse
import asyncio
import sys

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.core.config import settings
from src.core.security import (
    create_access_token,
    create_refresh_token,
    redis_refresh_key,
)
from src.models.user import User


async def issue_token(email: str) -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        result = await db.execute(select(User).where(User.email == email))
        user = result.scalar_one_or_none()

    await engine.dispose()

    if user is None:
        print(f"Error: no user found with email '{email}'.", file=sys.stderr)
        sys.exit(1)

    access_token, _ = create_access_token(user.id)
    refresh_token, jti = create_refresh_token(user.id)

    redis = aioredis.Redis.from_url(settings.redis_url)
    ttl = settings.refresh_token_expire_days * 86400
    await redis.setex(redis_refresh_key(user.id, jti), ttl, "1")
    await redis.aclose()

    print(f"User:          {user.id}  {user.email}")
    print(f"Access token:  {access_token}")
    print(f"Refresh token: {refresh_token}")
    print(f"Expires in:    {settings.access_token_expire_minutes * 60}s")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Issue a token pair for an existing user"
    )
    parser.add_argument(
        "--username", required=True, metavar="EMAIL", help="User email address"
    )
    args = parser.parse_args()

    asyncio.run(issue_token(email=args.username.strip().lower()))


if __name__ == "__main__":
    main()
