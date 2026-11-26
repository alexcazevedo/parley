"""Demo surface is off by default; outside demo mode, placeholder secrets
refuse to boot and the public demo tenant key authenticates nothing (PRD
E2.2, E2.3). Only the demo-key tests use the DB-backed `client` fixture.
"""

import os
import secrets

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from app.config import (
    PLACEHOLDER_ADMIN_API_KEY,
    InsecureSettingsError,
    Settings,
    assert_secure_settings,
    get_settings,
)
from app.main import create_app


def _secure_settings(**overrides) -> Settings:
    values = {
        "demo_mode": False,
        "admin_api_key": "k",
        "secrets_key": Fernet.generate_key().decode(),
    }
    values.update(overrides)
    return Settings(**values)


async def _client(settings: Settings) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=create_app(settings)), base_url="http://test")


@pytest.mark.parametrize(
    ("method", "path"),
    [("POST", "/chat/bootstrap"), ("GET", "/chat"), ("POST", "/dev/messages")],
)
async def test_demo_routes_absent_outside_demo_mode(method, path):
    async with await _client(_secure_settings()) as ac:
        response = await ac.request(method, path, json={})
    assert response.status_code == 404


@pytest.mark.parametrize("path", ["/health", "/operator"])
async def test_core_routes_present_outside_demo_mode(path):
    async with await _client(_secure_settings()) as ac:
        response = await ac.get(path)
    assert response.status_code == 200


async def test_demo_app_serves_chat_page():
    async with await _client(_secure_settings(demo_mode=True)) as ac:
        response = await ac.get("/chat")
    assert response.status_code == 200


def test_demo_mode_defaults_off(monkeypatch):
    monkeypatch.delenv("DEMO_MODE", raising=False)
    assert Settings(_env_file=None).demo_mode is False


def test_tool_max_response_bytes_default():
    assert Settings(_env_file=None).tool_max_response_bytes == 32768


def test_placeholder_admin_key_rejected():
    settings = _secure_settings(admin_api_key=PLACEHOLDER_ADMIN_API_KEY)
    with pytest.raises(InsecureSettingsError) as exc:
        assert_secure_settings(settings)
    assert "ADMIN_API_KEY" in str(exc.value)
    assert PLACEHOLDER_ADMIN_API_KEY not in str(exc.value)


def test_empty_admin_key_rejected():
    with pytest.raises(InsecureSettingsError, match="ADMIN_API_KEY"):
        assert_secure_settings(_secure_settings(admin_api_key=""))


def test_invalid_fernet_key_rejected():
    bad = "changeme-fernet-key-base64=="
    with pytest.raises(InsecureSettingsError) as exc:
        assert_secure_settings(_secure_settings(secrets_key=bad))
    assert "SECRETS_KEY" in str(exc.value)
    assert bad not in str(exc.value)


def test_secure_settings_pass():
    assert_secure_settings(_secure_settings())


def test_demo_mode_accepts_placeholders():
    settings = _secure_settings(
        demo_mode=True,
        admin_api_key=PLACEHOLDER_ADMIN_API_KEY,
        secrets_key="changeme-fernet-key-base64==",
    )
    assert_secure_settings(settings)


def test_create_app_refuses_insecure_settings():
    with pytest.raises(InsecureSettingsError):
        create_app(_secure_settings(admin_api_key=PLACEHOLDER_ADMIN_API_KEY))


# --- Demo tenant key outside demo mode (reviewer findings 2 and 11) ---------


@pytest.fixture
def demo_mode_off(monkeypatch):
    """Flip DEMO_MODE off for one test; restore the cached demo-mode settings."""
    get_settings.cache_clear()
    monkeypatch.setenv("DEMO_MODE", "false")
    get_settings.cache_clear()
    yield
    monkeypatch.setenv("DEMO_MODE", "true")
    get_settings.cache_clear()


async def _bootstrap_demo_key(client) -> str:
    response = await client.post("/chat/bootstrap")
    assert response.status_code == 200
    return response.json()["api_key"]


async def test_demo_key_works_in_demo_mode(client):
    demo_key = await _bootstrap_demo_key(client)
    response = await client.get("/agents", headers={"X-API-Key": demo_key})
    assert response.status_code == 200


async def test_demo_key_rejected_outside_demo_mode(client, monkeypatch):
    demo_key = await _bootstrap_demo_key(client)

    get_settings.cache_clear()
    monkeypatch.setenv("DEMO_MODE", "false")
    get_settings.cache_clear()
    try:
        assert get_settings().demo_mode is False
        demo = await client.get("/agents", headers={"X-API-Key": demo_key})
        unknown = await client.get(
            "/agents", headers={"X-API-Key": secrets.token_urlsafe(32)}
        )
    finally:
        monkeypatch.setenv("DEMO_MODE", "true")
        get_settings.cache_clear()

    assert unknown.status_code == 401
    assert demo.status_code == unknown.status_code
    # `request_id` differs per request; everything else must match.
    demo_error = {k: v for k, v in demo.json()["error"].items() if k != "request_id"}
    unknown_error = {k: v for k, v in unknown.json()["error"].items() if k != "request_id"}
    assert demo_error == unknown_error == {"code": "http_error", "message": "invalid API key"}


async def test_tenant_key_works_outside_demo_mode(client, demo_mode_off):
    created = await client.post(
        "/tenants",
        json={"name": "Real Tenant"},
        headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]},
    )
    assert created.status_code in (200, 201)
    response = await client.get(
        "/agents", headers={"X-API-Key": created.json()["api_key"]}
    )
    assert response.status_code == 200


async def test_worker_refuses_placeholder_admin_key_before_redis(monkeypatch):
    """Guard: passes on current code; covers the worker's boot refusal line."""
    import app.workers.main as worker_main

    settings = _secure_settings(admin_api_key=PLACEHOLDER_ADMIN_API_KEY)
    monkeypatch.setattr(worker_main, "get_settings", lambda: settings)

    def _no_redis(*args, **kwargs):
        raise AssertionError("Redis.from_url reached before settings check")

    monkeypatch.setattr(worker_main.Redis, "from_url", _no_redis)
    with pytest.raises(InsecureSettingsError):
        await worker_main.main()
