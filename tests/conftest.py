"""Shared pytest fixtures: an isolated test DB, fake Redis, and an ASGI client
with app dependencies overridden to use both.
"""

import ipaddress
import os
import socket
from collections.abc import AsyncGenerator

import asyncpg
import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from fakeredis import FakeAsyncRedis
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
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


_ALLOWED_HOSTS = {"localhost", "127.0.0.1", "::1"}
# The ASGI test client's host (`http://test`): answered here, never by real DNS.
_LOCAL_ANSWER_HOSTS = {"test"}


class NetworkGuard:
    """Refuses connections, datagrams and name lookups outside the test box.

    Blocked attempts are recorded, so a test whose application code swallowed
    the error still fails at teardown (`check`). `allowed_addresses` holds the
    addresses the configured database and Redis hosts resolved to: those may
    be connected to as well as loopback.
    """

    def __init__(self) -> None:
        self.attempts: list[str] = []
        self.allowed_addresses: set[str] = set()

    def refuse(self, host: str) -> None:
        self.attempts.append(host)
        raise OSError(f"network guard: test tried to reach {host!r}")

    def check(self) -> None:
        if self.attempts:
            hosts = ", ".join(sorted(set(self.attempts)))
            raise AssertionError(f"test reached outside hosts (network guard): {hosts}")

    def allows(self, address) -> bool:
        if isinstance(address, (str, bytes)):  # AF_UNIX path
            return True
        host = address[0]
        return (
            host in ("127.0.0.1", "::1")
            or host.startswith("::ffff:127.")
            or host in self.allowed_addresses
        )


def _service_hosts() -> set[str]:
    """Hosts of the configured test database and Redis URLs (lower case)."""
    from urllib.parse import urlsplit

    from app.config import get_settings

    settings = get_settings()
    hosts: set[str] = set()
    for url in (TEST_DATABASE_URL, settings.database_url, settings.redis_url):
        try:
            host = urlsplit(url).hostname
        except ValueError:
            continue
        if host:
            hosts.add(host.lower())
    return hosts


def _local_answer(port, family, socktype, proto):
    """`getaddrinfo`'s answer for a `_LOCAL_ANSWER_HOSTS` name: loopback, IPv4."""
    if isinstance(port, bytes):
        port = port.decode()
    number = int(port) if isinstance(port, int) or (port or "").isdigit() else 0
    return [
        (
            socket.AF_INET,
            socktype or socket.SOCK_STREAM,
            proto or socket.IPPROTO_TCP,
            "",
            ("127.0.0.1", number),
        )
    ]


@pytest.fixture(autouse=True)
def network_guard(monkeypatch, _stable_dns):
    """Block real outside traffic (decided at `socket.socket.connect`,
    `connect_ex`, `sendto` and at name lookups); `respx` and httpx mock/ASGI
    transports never open a socket and are unaffected. Lookups of `test` are
    answered with loopback here; lookups of the configured database and Redis
    hosts go to the resolver, and what they resolve to may be connected to.
    Fails the test at teardown if anything tried.
    """
    guard = NetworkGuard()
    lookup = socket.getaddrinfo  # `_stable_dns` is already in place
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sendto = socket.socket.sendto

    def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
        name = host.decode() if isinstance(host, bytes) else host
        lowered = name.lower() if isinstance(name, str) else None
        if lowered in _LOCAL_ANSWER_HOSTS:
            return _local_answer(port, family, type, proto)
        if lowered is not None and lowered in _service_hosts():
            answers = lookup(host, port, family, type, proto, flags)
            guard.allowed_addresses.update(str(info[4][0]) for info in answers)
            return answers
        if (
            lowered is not None
            and lowered not in _ALLOWED_HOSTS
            and not lowered.endswith(_STABLE_DNS_SUFFIX)
            and not _is_ip_literal(name)
        ):
            guard.refuse(name)
        return lookup(host, port, family, type, proto, flags)

    def connect(self, address):
        if not guard.allows(address):
            guard.refuse(str(address[0]))
        return real_connect(self, address)

    def connect_ex(self, address):
        if not guard.allows(address):
            guard.refuse(str(address[0]))
        return real_connect_ex(self, address)

    def sendto(self, data, *args):
        # `sendto(data, address)` or `sendto(data, flags, address)`.
        address = args[-1] if args else None
        if address is not None and not guard.allows(address):
            guard.refuse(str(address[0]))
        return real_sendto(self, data, *args)

    for host in _service_hosts():
        if _is_ip_literal(host):
            guard.allowed_addresses.add(host)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", sendto)
    yield guard
    guard.check()


def _is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


@pytest.fixture
def fake_handoff_brief(monkeypatch) -> str:
    """The operator brief of a handoff comes from a fake model, not the network.

    Tests whose turn hands off without injecting their own brief provider use
    this; it returns the brief text the fake produces.
    """
    import app.handoff.summarizer as summarizer_module
    from app.llm.base import LLMResponse
    from app.llm.providers.fake import FakeLLMProvider

    text = "Resumo do atendimento para o operador."
    monkeypatch.setattr(
        summarizer_module,
        "get_provider",
        lambda agent, settings=None: FakeLLMProvider([LLMResponse(content=text)]),
    )
    return text


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    await _ensure_test_database_exists()
    engine = create_async_engine(TEST_DATABASE_URL, pool_pre_ping=True)
    async with engine.begin() as conn:
        # Outside the ORM (app/knowledge/embeddings.py), created by the tests
        # that need it; dropped first since it references knowledge_passages.
        await conn.execute(text("DROP TABLE IF EXISTS knowledge_embeddings"))
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.execute(text("DROP TABLE IF EXISTS knowledge_embeddings"))
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
