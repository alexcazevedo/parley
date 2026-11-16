"""E2.5: request bodies may only reference agents/channels of the caller's tenant."""

import os
import uuid


async def _create_tenant(client, name: str) -> dict:
    response = await client.post(
        "/tenants", json={"name": name}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    assert response.status_code == 201
    return response.json()


def _auth(api_key: str) -> dict:
    return {"X-API-Key": api_key}


async def _agent(client, tenant: dict) -> str:
    r = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Hi.", "model": "gpt-4o-mini"},
        headers=_auth(tenant["api_key"]),
    )
    assert r.status_code == 201
    return r.json()["id"]


async def _channel(client, tenant: dict, agent_id: str) -> str:
    r = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent_id},
        headers=_auth(tenant["api_key"]),
    )
    assert r.status_code == 201
    return r.json()["id"]


async def test_create_channel_with_foreign_agent_is_422(client):
    a = await _create_tenant(client, "A")
    b = await _create_tenant(client, "B")
    agent_a = await _agent(client, a)
    r = await client.post(
        "/channels",
        json={"type": "webchat", "name": "W", "agent_id": agent_a},
        headers=_auth(b["api_key"]),
    )
    assert r.status_code == 422
    assert r.json()["error"]["message"] == "agent not found"


async def test_create_channel_with_random_agent_is_422(client):
    b = await _create_tenant(client, "B")
    r = await client.post(
        "/channels",
        json={"type": "webchat", "name": "W", "agent_id": str(uuid.uuid4())},
        headers=_auth(b["api_key"]),
    )
    assert r.status_code == 422
    assert r.json()["error"]["message"] == "agent not found"


async def test_patch_channel_to_foreign_agent_is_422_and_unchanged(client):
    a = await _create_tenant(client, "A")
    b = await _create_tenant(client, "B")
    agent_a = await _agent(client, a)
    agent_b = await _agent(client, b)
    channel_b = await _channel(client, b, agent_b)
    r = await client.patch(
        f"/channels/{channel_b}", json={"agent_id": agent_a}, headers=_auth(b["api_key"])
    )
    assert r.status_code == 422
    assert r.json()["error"]["message"] == "agent not found"
    got = await client.get(f"/channels/{channel_b}", headers=_auth(b["api_key"]))
    assert got.json()["agent_id"] == agent_b


async def test_patch_unknown_channel_stays_404(client):
    b = await _create_tenant(client, "B")
    r = await client.patch(
        f"/channels/{uuid.uuid4()}",
        json={"agent_id": str(uuid.uuid4())},
        headers=_auth(b["api_key"]),
    )
    assert r.status_code == 404


async def test_same_tenant_references_still_work(client):
    b = await _create_tenant(client, "B")
    agent1 = await _agent(client, b)
    agent2 = await _agent(client, b)
    channel = await _channel(client, b, agent1)
    r = await client.patch(
        f"/channels/{channel}", json={"agent_id": agent2}, headers=_auth(b["api_key"])
    )
    assert r.status_code == 200
    assert r.json()["agent_id"] == agent2
    r = await client.patch(
        f"/channels/{channel}", json={"name": "renamed"}, headers=_auth(b["api_key"])
    )
    assert r.status_code == 200
    c = await client.post(
        "/contacts",
        json={"channel_id": channel, "external_user_id": "u1"},
        headers=_auth(b["api_key"]),
    )
    assert c.status_code == 201


async def test_create_contact_with_foreign_channel_is_422(client):
    a = await _create_tenant(client, "A")
    b = await _create_tenant(client, "B")
    channel_a = await _channel(client, a, await _agent(client, a))
    r = await client.post(
        "/contacts",
        json={"channel_id": channel_a, "external_user_id": "u1"},
        headers=_auth(b["api_key"]),
    )
    assert r.status_code == 422
    assert r.json()["error"]["message"] == "channel not found"


async def test_create_contact_with_random_channel_is_422_not_409(client):
    b = await _create_tenant(client, "B")
    r = await client.post(
        "/contacts",
        json={"channel_id": str(uuid.uuid4()), "external_user_id": "u1"},
        headers=_auth(b["api_key"]),
    )
    assert r.status_code == 422
    assert r.json()["error"]["message"] == "channel not found"


async def test_duplicate_contact_still_409(client):
    b = await _create_tenant(client, "B")
    channel = await _channel(client, b, await _agent(client, b))
    body = {"channel_id": channel, "external_user_id": "dup"}
    first = await client.post("/contacts", json=body, headers=_auth(b["api_key"]))
    assert first.status_code == 201
    r = await client.post("/contacts", json=body, headers=_auth(b["api_key"]))
    assert r.status_code == 409


async def test_patch_channel_with_null_agent_id_is_ignored(client):
    b = await _create_tenant(client, "B")
    agent_b = await _agent(client, b)
    channel_b = await _channel(client, b, agent_b)
    r = await client.patch(
        f"/channels/{channel_b}", json={"agent_id": None}, headers=_auth(b["api_key"])
    )
    assert r.status_code == 200
    assert r.json()["agent_id"] == agent_b
    got = await client.get(f"/channels/{channel_b}", headers=_auth(b["api_key"]))
    assert got.json()["agent_id"] == agent_b
