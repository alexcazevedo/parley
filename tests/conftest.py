"""Shared pytest fixtures: an isolated test DB, fake Redis, and an ASGI client
with app dependencies overridden to use both.
"""

import os
import socket
from collections.abc import AsyncGenerator

import asyncpg
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fakeredis import FakeAsyncRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# `harness_test` is a separate database from the one docker-compose's api/
# worker use, so tests never touch dev data. Override via env var if needed.
TEST_DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+asyncpg://harness:harness@localhost:5434/harness_test"
)
# asyncpg (used for the CREATE DATABASE bootstrap below) needs a plain
# postgres:// DSN, not the SQLAlchemy `postgresql+asyncpg://` one.
_ASYNCPG_ADMIN_DSN = TEST_DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://").rsplit(
    "/", 1
)[0]
_TEST_DB_NAME = TEST_DATABASE_URL.rsplit("/", 1)[1]

os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)
os.environ.setdefault("ADMIN_API_KEY", "test-admin-key")
# Existing tests use the demo surface (/chat/bootstrap, /dev/messages).
os.environ.setdefault("DEMO_MODE", "true")
# No real waiting between retries of a 429/5xx (app/http_retry.py).
os.environ.setdefault("HTTP_RETRY_BASE_SECONDS", "0")


def _ensure_valid_secrets_key() -> None:
    """`.env`'s SECRETS_KEY is a human-readable placeholder, not a real Fernet
    key. Environment variables take priority over `.env` in pydantic-settings,
    so setting a generated key here overrides it for the whole test session.
    """
    key = os.environ.get("SECRETS_KEY")
    if key:
        try:
            Fernet(key.encode("utf-8"))
            return  # already a valid Fernet key (e.g. set explicitly for CI)
        except Exception:
            pass
    os.environ["SECRETS_KEY"] = Fernet.generate_key().decode("utf-8")


_ensure_valid_secrets_key()

from app.api.deps import get_db, get_redis  # noqa: E402
from app.http_client import close_shared_client  # noqa: E402
from app.main import app  # noqa: E402
from app.models.base import Base  # noqa: E402


async def _ensure_test_database_exists() -> None:
    conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/postgres")
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", _TEST_DB_NAME
        )
        if not exists:
            await conn.execute(f'CREATE DATABASE "{_TEST_DB_NAME}"')
    finally:
        await conn.close()


_STABLE_DNS_SUFFIX = ".example.com"
_STABLE_DNS_ADDRESS = "93.184.216.34"


@pytest_asyncio.fixture(autouse=True)
async def _close_shared_http_client():
    """Tests run one event loop each; the shared client must not outlive its loop."""
    yield
    await close_shared_client()


@pytest.fixture(autouse=True)
def _stable_dns(monkeypatch):
    """Channel calls resolve their `base_url` before every request (SSRF
    check). Make `*.example.com` (what Evolution tests use) resolve to a
    public address without any real DNS; every other host goes to the real
    `socket.getaddrinfo`. Tests that need other answers patch it themselves.
    """
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        if isinstance(host, bytes):
            host = host.decode()
        if isinstance(host, str) and host.lower().endswith(_STABLE_DNS_SUFFIX):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", (_STABLE_DNS_ADDRESS, port or 0))
            ]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    await _ensure_test_database_exists()
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    session_maker = async_sessionmaker(bind=test_engine, expire_on_commit=False)
    async with session_maker() as session:
        yield session
        await session.rollback()


@pytest_asyncio.fixture
async def fake_redis() -> AsyncGenerator[FakeAsyncRedis, None]:
    redis = FakeAsyncRedis(decode_responses=True)
    yield redis
    await redis.flushall()
    await redis.aclose()


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession, fake_redis: FakeAsyncRedis
) -> AsyncGenerator[AsyncClient, None]:
    async def _get_db_override():
        yield db_session

    async def _get_redis_override():
        return fake_redis

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_redis] = _get_redis_override

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
