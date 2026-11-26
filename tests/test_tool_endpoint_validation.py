"""A tool endpoint is checked by the SSRF guard when the tool is created or
updated, not only when it is called (PRD E2.8). The check is static (no DNS),
so these tests never resolve anything.
"""

import socket
import uuid

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.tool import Tool
from tests.test_ingest import _auth, _create_tenant

UNSAFE_ENDPOINTS = [
    "http://100.64.0.1/x",
    "http://127.0.0.1/x",
    "file:///etc/passwd",
    "http://localhost/x",
    "http://foo.localhost/x",
    "http://printer.local/x",
    "http://metadata.google.internal/",
    "http://127.1/",
    "http://0x7f000001/",
    "http://2130706433/",
    "http://0177.0.0.1/",
    "http://[::7f00:1]/",
    "http://[fec0::1]/",
]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("TOOL_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("TOOL_SSRF_DISABLED", raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def no_dns(monkeypatch):
    def getaddrinfo(*args, **kwargs):
        raise AssertionError("endpoint validation must not resolve DNS")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


async def _tool_rows(db_session, tenant_id: str) -> list[Tool]:
    result = await db_session.execute(select(Tool).where(Tool.tenant_id == uuid.UUID(tenant_id)))
    return list(result.scalars().all())


@pytest.mark.parametrize("endpoint", UNSAFE_ENDPOINTS)
async def test_create_rejects_unsafe_endpoint(client, db_session, endpoint):
    tenant = await _create_tenant(client)
    response = await client.post(
        "/tools",
        json={"name": "get_order", "endpoint": endpoint},
        headers=_auth(tenant["api_key"]),
    )
    assert response.status_code == 422
    assert response.json()["error"]["message"].startswith("invalid endpoint: ")
    assert await _tool_rows(db_session, tenant["id"]) == []


@pytest.mark.parametrize("endpoint", UNSAFE_ENDPOINTS)
async def test_update_rejects_unsafe_endpoint_and_keeps_old_one(client, endpoint):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    created = await client.post(
        "/tools",
        json={"name": "get_order", "endpoint": "https://example.com/orders"},
        headers=headers,
    )
    assert created.status_code == 201
    tool_id = created.json()["id"]

    response = await client.patch(
        f"/tools/{tool_id}", json={"endpoint": endpoint}, headers=headers
    )
    assert response.status_code == 422
    assert response.json()["error"]["message"].startswith("invalid endpoint: ")

    current = await client.get(f"/tools/{tool_id}", headers=headers)
    assert current.json()["endpoint"] == "https://example.com/orders"


async def test_update_without_endpoint_is_not_validated(client):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    created = await client.post(
        "/tools",
        json={"name": "get_order", "endpoint": "https://example.com/orders"},
        headers=headers,
    )
    response = await client.patch(
        f"/tools/{created.json()['id']}", json={"description": "x"}, headers=headers
    )
    assert response.status_code == 200


async def test_create_accepts_public_hostname(client):
    tenant = await _create_tenant(client)
    response = await client.post(
        "/tools",
        json={"name": "get_order", "endpoint": "https://example.com/orders"},
        headers=_auth(tenant["api_key"]),
    )
    assert response.status_code == 201


async def test_allowlist_is_authoritative_at_create(client, monkeypatch):
    monkeypatch.setenv("TOOL_ALLOWED_HOSTS", "mock-tools")
    get_settings.cache_clear()
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])

    allowed = await client.post(
        "/tools",
        json={"name": "get_order", "endpoint": "http://mock-tools:9000/orders"},
        headers=headers,
    )
    assert allowed.status_code == 201

    refused = await client.post(
        "/tools",
        json={"name": "other", "endpoint": "https://example.com/x"},
        headers=headers,
    )
    assert refused.status_code == 422
    assert "TOOL_ALLOWED_HOSTS" in refused.json()["error"]["message"]


async def test_allowlisted_internal_name_is_accepted_at_create(client, monkeypatch):
    monkeypatch.setenv("TOOL_ALLOWED_HOSTS", "billing.internal")
    get_settings.cache_clear()
    tenant = await _create_tenant(client)
    response = await client.post(
        "/tools",
        json={"name": "get_order", "endpoint": "http://billing.internal/orders"},
        headers=_auth(tenant["api_key"]),
    )
    assert response.status_code == 201
