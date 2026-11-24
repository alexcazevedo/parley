"""E2.10: tenant-wide atomic inbound rate limits; rate-limited webhooks lose nothing."""

import copy
import json
import uuid
from pathlib import Path

import pytest
import structlog.testing
from sqlalchemy import select

from app.config import get_settings
from app.models.message import Message
from app.queue.redis_queue import JOBS_KEY
from tests.test_ingest import _auth, _create_channel, _create_tenant
from tests.test_webhook_security import _create_evolution_channel

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def tenant_limit(monkeypatch):
    def _set(value: int) -> None:
        monkeypatch.setenv("RATE_LIMIT_TENANT_PER_MIN", str(value))
        get_settings.cache_clear()

    yield _set
    monkeypatch.delenv("RATE_LIMIT_TENANT_PER_MIN", raising=False)
    get_settings.cache_clear()


def _payload(name: str, message_id: str) -> dict:
    payload = copy.deepcopy(json.loads((FIXTURES / name).read_text()))
    payload["data"]["key"]["id"] = message_id
    return payload


async def test_tenant_limit_trips_across_different_users(client, fake_redis, tenant_limit):
    tenant_limit(2)
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_channel(client, headers)

    statuses = []
    for user in ("user-a", "user-b", "user-c"):
        response = await client.post(
            "/channels/webchat/messages",
            json={"channel_id": channel_id, "user_id": user, "message": "hi"},
            headers=headers,
        )
        statuses.append(response.status_code)
    assert statuses == [201, 201, 429]


async def test_over_limit_webhook_stores_message_without_job(
    client, fake_redis, db_session, tenant_limit
):
    tenant_limit(1)
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers, "evo-secret")

    first = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_payload("evolution_messages_upsert.json", "MSG-1"),
        headers={"apikey": "evo-secret"},
    )
    assert first.status_code == 200
    assert first.json() == {"status": "ok"}
    await fake_redis.delete(JOBS_KEY)

    with structlog.testing.capture_logs() as logs:
        second = await client.post(
            f"/webhooks/whatsapp/{channel_id}",
            json=_payload("evolution_messages_upsert.json", "MSG-2"),
            headers={"apikey": "evo-secret"},
        )
    assert second.status_code == 200
    assert second.json() == {"status": "rate_limited"}
    assert await fake_redis.llen(JOBS_KEY) == 0
    assert any(e["event"] == "webhook.rate_limited" for e in logs)

    rows = (
        await db_session.execute(
            select(Message).where(Message.channel_id == uuid.UUID(channel_id))
        )
    ).scalars().all()
    assert sorted(r.external_message_id for r in rows) == ["MSG-1", "MSG-2"]


async def test_over_limit_media_webhook_skips_fetch(
    client, fake_redis, db_session, tenant_limit, monkeypatch
):
    from app.channels.evolution import EvolutionAdapter

    calls: list[str] = []

    async def _no_fetch(self, incoming, channel):
        calls.append(incoming.external_message_id)
        raise AssertionError("fetch_media must not run when rate limited")

    monkeypatch.setattr(EvolutionAdapter, "fetch_media", _no_fetch)
    tenant_limit(0)
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers, "evo-secret")

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_payload("evolution_image_upsert.json", "IMG-1"),
        headers={"apikey": "evo-secret"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "rate_limited"}
    assert calls == []
    assert await fake_redis.llen(JOBS_KEY) == 0

    row = (
        await db_session.execute(
            select(Message).where(Message.channel_id == uuid.UUID(channel_id))
        )
    ).scalar_one()
    assert row.type == "unsupported"
    assert row.content == "[mídia não recebida]"
    assert row.media_key is None


async def test_under_limit_webhook_enqueues(client, fake_redis):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers, "evo-secret")

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_payload("evolution_messages_upsert.json", "MSG-OK"),
        headers={"apikey": "evo-secret"},
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert await fake_redis.llen(JOBS_KEY) == 1


class _SpyPipeline:
    def __init__(self, real, log: list):
        self._real = real
        self._log = log

    async def __aenter__(self):
        await self._real.__aenter__()
        return self

    async def __aexit__(self, *exc):
        return await self._real.__aexit__(*exc)

    def incr(self, *args, **kwargs):
        self._log.append("incr")
        return self._real.incr(*args, **kwargs)

    def expire(self, *args, **kwargs):
        self._log.append("expire")
        return self._real.expire(*args, **kwargs)

    async def execute(self, *args, **kwargs):
        self._log.append("execute")
        return await self._real.execute(*args, **kwargs)


class _SpyRedis:
    """Only `pipeline` is exposed; direct `incr`/`expire` calls fail loudly."""

    def __init__(self, real):
        self._real = real
        self.pipeline_kwargs: list[dict] = []
        self.log: list[str] = []

    def pipeline(self, *args, **kwargs):
        self.pipeline_kwargs.append({"args": args, **kwargs})
        return _SpyPipeline(self._real.pipeline(*args, **kwargs), self.log)

    def __getattr__(self, name):
        raise AssertionError(f"redis.{name} called outside the MULTI/EXEC pipeline")


async def test_hit_rate_limit_sets_ttl_and_reports_over(fake_redis):
    from app.security.ratelimit import hit_rate_limit

    spy = _SpyRedis(fake_redis)
    assert await hit_rate_limit(spy, "tenant", "t1", 1) is False
    # INCR and EXPIRE are queued on one MULTI/EXEC pipeline, then one execute().
    assert len(spy.pipeline_kwargs) == 1
    assert spy.pipeline_kwargs[0].get("transaction") is True
    assert spy.log == ["incr", "expire", "execute"]

    keys = await fake_redis.keys("harness:rl:tenant:t1:*")
    assert len(keys) == 1
    ttl = await fake_redis.ttl(keys[0])
    assert 0 < ttl <= 120
    assert await hit_rate_limit(fake_redis, "tenant", "t1", 1) is True


@pytest.fixture
def user_limit(monkeypatch):
    def _set(value: int) -> None:
        monkeypatch.setenv("RATE_LIMIT_USER_PER_MIN", str(value))
        get_settings.cache_clear()

    yield _set
    monkeypatch.delenv("RATE_LIMIT_USER_PER_MIN", raising=False)
    get_settings.cache_clear()


async def test_spamming_user_does_not_exhaust_tenant_budget(
    fake_redis, tenant_limit, user_limit
):
    from app.security.ratelimit import inbound_over_limit

    tenant_limit(5)
    user_limit(2)
    tenant_id, channel_id = uuid.uuid4(), uuid.uuid4()

    for _ in range(20):
        await inbound_over_limit(fake_redis, tenant_id, channel_id, "spammer")
    assert await inbound_over_limit(fake_redis, tenant_id, channel_id, "spammer") is True
    assert await inbound_over_limit(fake_redis, tenant_id, channel_id, "legit") is False


async def test_tenant_limit_enforced_across_users(fake_redis, tenant_limit, user_limit):
    from app.security.ratelimit import inbound_over_limit

    tenant_limit(5)
    user_limit(2)
    tenant_id, channel_id = uuid.uuid4(), uuid.uuid4()

    results = [
        await inbound_over_limit(fake_redis, tenant_id, channel_id, f"user-{n}")
        for n in range(6)
    ]
    assert results == [False] * 5 + [True]
