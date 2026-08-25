"""CRUD REST coverage for agents, channels, tools, contacts, plus read-only
conversations and the Fernet round-trip used to encrypt secrets at rest.
"""

import os
import uuid

from app.security import secrets as secrets_crypto


def _admin_key() -> str:
    return os.environ["ADMIN_API_KEY"]


async def _create_tenant(client, name: str = "Acme") -> dict:
    response = await client.post(
        "/tenants", json={"name": name}, headers={"X-Admin-Key": _admin_key()}
    )
    assert response.status_code == 201
    return response.json()


def _auth(api_key: str) -> dict:
    return {"X-API-Key": api_key}


def test_secrets_round_trip():
    plaintext = "shh-super-secret"
    token = secrets_crypto.encrypt(plaintext)
    assert token != plaintext
    assert secrets_crypto.decrypt(token) == plaintext


async def test_agent_crud_lifecycle(client):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])

    create = await client.post(
        "/agents",
        json={
            "name": "Support Bot",
            "instructions": "Be helpful.",
            "model": "gpt-4o-mini",
        },
        headers=headers,
    )
    assert create.status_code == 201
    body = create.json()
    assert body["provider"] == "openai_compatible"
    assert body["max_tokens"] == 1024
    assert body["max_tool_iterations"] == 5
    assert body["context_message_limit"] == 20
    assert body["summary_threshold"] == 40
    assert body["tool_names"] == []
    agent_id = body["id"]

    listed = await client.get("/agents", headers=headers)
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    got = await client.get(f"/agents/{agent_id}", headers=headers)
    assert got.status_code == 200
    assert got.json()["name"] == "Support Bot"

    patched = await client.patch(
        f"/agents/{agent_id}", json={"temperature": 0.2}, headers=headers
    )
    assert patched.status_code == 200
    assert patched.json()["temperature"] == 0.2

    deleted = await client.delete(f"/agents/{agent_id}", headers=headers)
    assert deleted.status_code == 204

    gone = await client.get(f"/agents/{agent_id}", headers=headers)
    assert gone.status_code == 404


async def test_channel_crud_and_secret_never_returned(client):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])

    agent = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Hi.", "model": "gpt-4o-mini"},
        headers=headers,
    )
    agent_id = agent.json()["id"]

    create = await client.post(
        "/channels",
        json={
            "type": "webchat",
            "name": "Web Widget",
            "agent_id": agent_id,
            "secret": "webhook-secret",
        },
        headers=headers,
    )
    assert create.status_code == 201
    body = create.json()
    assert "secret" not in body
    assert "secret_encrypted" not in body
    channel_id = body["id"]

    got = await client.get(f"/channels/{channel_id}", headers=headers)
    assert got.status_code == 200
    assert "secret" not in got.json()

    patched = await client.patch(
        f"/channels/{channel_id}", json={"name": "Renamed"}, headers=headers
    )
    assert patched.status_code == 200
    assert patched.json()["name"] == "Renamed"

    deleted = await client.delete(f"/channels/{channel_id}", headers=headers)
    assert deleted.status_code == 204


async def test_tool_unique_name_per_tenant(client):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])

    first = await client.post(
        "/tools",
        json={"name": "get_order", "endpoint": "https://example.com/orders"},
        headers=headers,
    )
    assert first.status_code == 201

    duplicate = await client.post(
        "/tools",
        json={"name": "get_order", "endpoint": "https://example.com/orders-v2"},
        headers=headers,
    )
    assert duplicate.status_code == 409


async def test_tool_unique_name_scoped_per_tenant_not_global(client):
    tenant_a = await _create_tenant(client, "Tenant A")
    tenant_b = await _create_tenant(client, "Tenant B")

    a = await client.post(
        "/tools",
        json={"name": "get_order", "endpoint": "https://example.com/orders"},
        headers=_auth(tenant_a["api_key"]),
    )
    assert a.status_code == 201

    b = await client.post(
        "/tools",
        json={"name": "get_order", "endpoint": "https://example.com/orders"},
        headers=_auth(tenant_b["api_key"]),
    )
    assert b.status_code == 201


async def test_contact_crud_and_unique_per_channel(client):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])

    agent = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Hi.", "model": "gpt-4o-mini"},
        headers=headers,
    )
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    channel_id = channel.json()["id"]

    create = await client.post(
        "/contacts",
        json={"channel_id": channel_id, "external_user_id": "user-123", "display_name": "Ana"},
        headers=headers,
    )
    assert create.status_code == 201
    contact_id = create.json()["id"]

    duplicate = await client.post(
        "/contacts",
        json={"channel_id": channel_id, "external_user_id": "user-123"},
        headers=headers,
    )
    assert duplicate.status_code == 409

    patched = await client.patch(
        f"/contacts/{contact_id}", json={"display_name": "Ana M."}, headers=headers
    )
    assert patched.status_code == 200
    assert patched.json()["display_name"] == "Ana M."


async def test_conversations_are_read_only_and_empty_by_default(client):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])

    listed = await client.get("/conversations", headers=headers)
    assert listed.status_code == 200
    assert listed.json() == []

    missing = await client.get(f"/conversations/{uuid.uuid4()}", headers=headers)
    assert missing.status_code == 404


async def test_all_endpoints_require_api_key(client):
    for method, path in [
        ("GET", "/agents"),
        ("GET", "/channels"),
        ("GET", "/tools"),
        ("GET", "/contacts"),
        ("GET", "/conversations"),
    ]:
        response = await client.request(method, path)
        assert response.status_code == 401
