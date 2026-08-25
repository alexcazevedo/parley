import os


def _admin_key() -> str:
    return os.environ["ADMIN_API_KEY"]


async def test_create_tenant_requires_admin_key(client):
    response = await client.post("/tenants", json={"name": "Acme"})
    assert response.status_code == 401

    response = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": "wrong"}
    )
    assert response.status_code == 401


async def test_create_tenant_and_read_self(client):
    response = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": _admin_key()}
    )
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Acme"
    assert "id" in body
    api_key = body["api_key"]
    assert api_key

    me = await client.get("/tenants/me", headers={"X-API-Key": api_key})
    assert me.status_code == 200
    me_body = me.json()
    assert me_body["id"] == body["id"]
    assert me_body["name"] == "Acme"
    assert "api_key" not in me_body


async def test_read_self_wrong_key_is_unauthorized(client):
    response = await client.get("/tenants/me", headers={"X-API-Key": "not-a-real-key"})
    assert response.status_code == 401

    response = await client.get("/tenants/me")
    assert response.status_code == 401
