"""
Shared fixtures for integration tests.

The test database (default: openclaw_test) is created automatically before
the session and dropped after. Override with env var TEST_DATABASE_URL.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from src.core.db import get_db
from src.core.redis import get_redis
from src.core.security import create_access_token
from src.models import Base, User, Workspace, WorkspaceMember, WorkspaceRole

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://openclaw:openclaw@localhost:5432/openclaw_test",
)

# ---------------------------------------------------------------------------
# Database — create/drop the test DB around the whole session
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(scope="session")
async def _test_db():
    """Create the test database before the session and drop it after."""
    url = make_url(TEST_DATABASE_URL)
    db_name = url.database
    # Use the main app database as the admin connection — it's always present.
    # (The postgres maintenance DB may not be reachable with app credentials.)
    main_db = db_name.replace("_test", "") if db_name.endswith("_test") else "openclaw"
    admin_url = url.set(database=main_db)

    # Pass the URL object directly — str(url) masks the password as "***"
    eng = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with eng.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        await conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    await eng.dispose()

    yield

    eng2 = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    async with eng2.connect() as conn:
        await conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
    await eng2.dispose()


@pytest_asyncio.fixture(scope="session")
async def engine(_test_db):
    eng = create_async_engine(make_url(TEST_DATABASE_URL), pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def db_session(engine) -> AsyncGenerator[AsyncSession, None]:
    """
    Each test runs inside a transaction that is always rolled back.
    App code that calls session.commit() will create/release a savepoint
    (due to join_transaction_mode="create_savepoint") so the data is
    visible within the test but never reaches the actual DB.
    """
    connection = await engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )
    yield session
    await session.close()
    await transaction.rollback()
    await connection.close()


# ---------------------------------------------------------------------------
# Redis — in-process fake
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def fake_redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


# ---------------------------------------------------------------------------
# External service mocks — autouse so every test is isolated
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_external_services():
    """Prevent real NATS / Meilisearch / S3 calls in every test."""
    with (
        patch("src.services.nats.connect", new=AsyncMock()),
        patch("src.services.nats.close", new=AsyncMock()),
        patch("src.services.meili.init", new=AsyncMock()),
        patch("src.services.meili.close", new=AsyncMock()),
        patch("src.services.s3.upload_fileobj", new=AsyncMock()),
        patch("src.services.s3.delete_object", new=AsyncMock()),
        patch(
            "src.services.s3.generate_presigned_download_url",
            new=AsyncMock(return_value="http://fake-presigned-url"),
        ),
    ):
        yield


# ---------------------------------------------------------------------------
# FastAPI app + HTTP client
# ---------------------------------------------------------------------------


@pytest.fixture
def app(db_session, fake_redis):
    from src.main import app as fastapi_app

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def override_get_redis():
        yield fake_redis

    fastapi_app.dependency_overrides[get_db] = override_get_db
    fastapi_app.dependency_overrides[get_redis] = override_get_redis
    yield fastapi_app
    fastapi_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Common domain fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def test_user(db_session) -> User:
    user = User(email="testuser@example.com", display_name="Test User")
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user


@pytest.fixture
def auth_headers(test_user: User) -> dict[str, str]:
    token, _ = create_access_token(test_user.id)
    return {"Authorization": f"Bearer {token}"}


@pytest_asyncio.fixture
async def test_workspace(db_session, test_user: User) -> Workspace:
    ws = Workspace(name="Test Workspace", created_by=test_user.id)
    db_session.add(ws)
    await db_session.flush()

    member = WorkspaceMember(
        workspace_id=ws.id,
        user_id=test_user.id,
        role=WorkspaceRole.owner,
    )
    db_session.add(member)
    await db_session.flush()
    await db_session.refresh(ws)
    return ws
