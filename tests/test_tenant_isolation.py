"""Cross-tenant isolation: tenant B must never see tenant A's rows, and a
tool's `auth_config` must never round-trip through the API.
"""

import os


def _admin_key() -> str:
    return os.environ["ADMIN_API_KEY"]


async def _create_tenant(client, name: str) -> dict:
    response = await client.post(
        "/tenants", json={"name": name}, headers={"X-Admin-Key": _admin_key()}
    )
    assert response.status_code == 201
    return response.json()


def _auth(api_key: str) -> dict:
    return {"X-API-Key": api_key}


async def test_tenant_b_cannot_list_or_get_tenant_a_agent(client):
    tenant_a = await _create_tenant(client, "Tenant A")
    tenant_b = await _create_tenant(client, "Tenant B")

    created = await client.post(
        "/agents",
        json={"name": "Support Bot", "instructions": "Be helpful.", "model": "gpt-4o-mini"},
        headers=_auth(tenant_a["api_key"]),
    )
    assert created.status_code == 201
    agent_id = created.json()["id"]

    b_list = await client.get("/agents", headers=_auth(tenant_b["api_key"]))
    assert b_list.status_code == 200
    assert b_list.json() == []

    b_get = await client.get(f"/agents/{agent_id}", headers=_auth(tenant_b["api_key"]))
    assert b_get.status_code == 404

    a_get = await client.get(f"/agents/{agent_id}", headers=_auth(tenant_a["api_key"]))
    assert a_get.status_code == 200


async def test_tenant_b_cannot_get_tenant_a_tool_and_auth_config_never_returned(client):
    tenant_a = await _create_tenant(client, "Tenant A")
    tenant_b = await _create_tenant(client, "Tenant B")

    created = await client.post(
        "/tools",
        json={
            "name": "get_order",
            "description": "Look up an order by id",
            "endpoint": "https://example.com/orders",
            "auth_config": {"header": "X-Api-Key", "value": "super-secret"},
        },
        headers=_auth(tenant_a["api_key"]),
    )
    assert created.status_code == 201
    body = created.json()
    tool_id = body["id"]
    assert "auth_config" not in body
    assert "auth_config_encrypted" not in body

    b_get = await client.get(f"/tools/{tool_id}", headers=_auth(tenant_b["api_key"]))
    assert b_get.status_code == 404

    b_list = await client.get("/tools", headers=_auth(tenant_b["api_key"]))
    assert b_list.status_code == 200
    assert b_list.json() == []

    a_get = await client.get(f"/tools/{tool_id}", headers=_auth(tenant_a["api_key"]))
    assert a_get.status_code == 200
    assert "auth_config" not in a_get.json()
    assert "auth_config_encrypted" not in a_get.json()


async def test_tenant_b_cannot_delete_or_update_tenant_a_resources(client):
    tenant_a = await _create_tenant(client, "Tenant A")
    tenant_b = await _create_tenant(client, "Tenant B")

    created = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Hi.", "model": "gpt-4o-mini"},
        headers=_auth(tenant_a["api_key"]),
    )
    agent_id = created.json()["id"]

    patch = await client.patch(
        f"/agents/{agent_id}", json={"name": "Hijacked"}, headers=_auth(tenant_b["api_key"])
    )
    assert patch.status_code == 404

    delete = await client.delete(f"/agents/{agent_id}", headers=_auth(tenant_b["api_key"]))
    assert delete.status_code == 404

    still_there = await client.get(f"/agents/{agent_id}", headers=_auth(tenant_a["api_key"]))
    assert still_there.status_code == 200
    assert still_there.json()["name"] == "Bot"
