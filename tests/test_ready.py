"""GET /ready: readiness of the database and Redis."""

import asyncio

import app.api.health as health_module
from app.api.deps import get_db, get_redis
from app.main import app

OK = {"database": "ok", "redis": "ok"}


class BrokenDb:
    async def execute(self, *args, **kwargs):
        raise RuntimeError("secret-db-detail")


class BrokenRedis:
    async def ping(self):
        raise RuntimeError("secret-redis-detail")


class HangingRedis:
    async def ping(self):
        await asyncio.sleep(30)


def _override(dep, obj):
    async def _dep():
        return obj

    app.dependency_overrides[dep] = _dep


async def test_ready_ok(client):
    resp = await client.get("/ready")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ready", "checks": OK}


async def test_ready_database_failing(client):
    _override(get_db, BrokenDb())
    resp = await client.get("/ready")
    assert resp.status_code == 503
    assert resp.json() == {
        "status": "not_ready",
        "checks": {"database": "failed", "redis": "ok"},
    }
    assert "secret-db-detail" not in resp.text


async def test_ready_redis_failing(client):
    _override(get_redis, BrokenRedis())
    resp = await client.get("/ready")
    assert resp.status_code == 503
    assert resp.json() == {
        "status": "not_ready",
        "checks": {"database": "ok", "redis": "failed"},
    }
    assert "secret-redis-detail" not in resp.text


async def test_ready_both_failing(client):
    _override(get_db, BrokenDb())
    _override(get_redis, BrokenRedis())
    resp = await client.get("/ready")
    assert resp.status_code == 503
    assert resp.json() == {
        "status": "not_ready",
        "checks": {"database": "failed", "redis": "failed"},
    }


async def test_ready_redis_timeout(client, monkeypatch):
    monkeypatch.setattr(health_module, "CHECK_TIMEOUT", 0.05)
    _override(get_redis, HangingRedis())
    resp = await asyncio.wait_for(client.get("/ready"), timeout=5)
    assert resp.status_code == 503
    assert resp.json()["checks"] == {"database": "ok", "redis": "failed"}


async def test_ready_needs_no_api_key_and_health_unchanged(client):
    assert (await client.get("/ready")).status_code == 200
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
