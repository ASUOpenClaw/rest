#!/usr/bin/env python
"""
Create a local email/password user.

Usage (from rest/ directory):
    uv run python scripts/create_user.py --username admin@example.com --password secret
    uv run python scripts/create_user.py --username admin@example.com --password secret --display-name "Admin"
"""

import argparse
import asyncio
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.core.config import settings
from src.core.security import hash_password
from src.models.user import User


async def create_user(email: str, password: str, display_name: str | None) -> None:
    engine = create_async_engine(settings.database_url, echo=False)
    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as db:
        result = await db.execute(select(User).where(User.email == email))
        if result.scalar_one_or_none() is not None:
            print(f"Error: user with email '{email}' already exists.", file=sys.stderr)
            await engine.dispose()
            sys.exit(1)

        user = User(
            email=email,
            display_name=display_name or email.split("@")[0],
            password_hash=hash_password(password),
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)

    await engine.dispose()
    print(f"Created user: {user.id}  {user.email}  ({user.display_name})")


def main() -> None:
    parser = argparse.ArgumentParser(description="Create an email/password user")
    parser.add_argument(
        "--username", required=True, metavar="EMAIL", help="User email address"
    )
    parser.add_argument(
        "--password", required=True, help="Plaintext password (will be hashed)"
    )
    parser.add_argument(
        "--display-name",
        default=None,
        help="Display name (defaults to email local part)",
    )
    args = parser.parse_args()

    asyncio.run(
        create_user(
            email=args.username.strip().lower(),
            password=args.password,
            display_name=args.display_name,
        )
    )


if __name__ == "__main__":
    main()
