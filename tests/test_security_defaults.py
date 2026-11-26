"""Success-demo clauses proven on a default-configured (non-demo) app.

Clauses already asserted on the non-demo app elsewhere (XSS, boot failure,
webhook identity, uploads, SSRF at call time, tool truncation, rate limits)
are not repeated here.
"""

import os
import uuid

import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_db, get_redis
from app.config import Settings
from app.main import create_app


@pytest_asyncio.fixture
async def secure_client(db_session, fake_redis):
    settings = Settings(
        demo_mode=False,
        admin_api_key="k",
        secrets_key=Fernet.generate_key().decode(),
    )
    secure_app = create_app(settings)

    async def _db():
        yield db_session

    async def _redis():
        return fake_redis

    secure_app.dependency_overrides[get_db] = _db
    secure_app.dependency_overrides[get_redis] = _redis
    async with AsyncClient(transport=ASGITransport(app=secure_app), base_url="http://test") as ac:
        yield ac


async def _tenant(ac) -> dict:
    r = await ac.post(
        "/tenants",
        json={"name": f"T-{uuid.uuid4().hex[:8]}"},
        # Auth reads the admin key from get_settings() (env), not the Settings
        # passed to create_app.
        headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]},
    )
    assert r.status_code == 201
    return r.json()


async def test_demo_routes_are_404(secure_client):
    assert (await secure_client.post("/chat/bootstrap")).status_code == 404
    assert (await secure_client.post("/dev/messages", json={})).status_code == 404
    assert (await secure_client.get("/chat")).status_code == 404


async def test_admin_key_creates_tenant(secure_client):
    tenant = await _tenant(secure_client)
    assert tenant["api_key"]


async def test_cross_tenant_agent_is_422(secure_client):
    a = await _tenant(secure_client)
    b = await _tenant(secure_client)
    agent = await secure_client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Hi.", "model": "gpt-4o-mini"},
        headers={"X-API-Key": a["api_key"]},
    )
    assert agent.status_code == 201
    r = await secure_client.post(
        "/channels",
        json={"type": "webchat", "name": "W", "agent_id": agent.json()["id"]},
        headers={"X-API-Key": b["api_key"]},
    )
    assert r.status_code == 422


async def test_tool_at_cgnat_address_is_422(secure_client):
    tenant = await _tenant(secure_client)
    r = await secure_client.post(
        "/tools",
        json={
            "name": "t",
            "description": "d",
            "endpoint": "http://100.64.0.1/x",
            "parameters_schema": {"type": "object", "properties": {}},
        },
        headers={"X-API-Key": tenant["api_key"]},
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["message"].startswith("invalid endpoint")
