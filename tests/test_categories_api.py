"""`/categories` CRUD: tenant scoping, name rules, and delete leaving conversations in place."""

import os
import uuid

from sqlalchemy import select

from app.models.conversation import Conversation
from tests.test_operator_api import _drain_jobs, _send, _setup


async def _tenant(client, name: str) -> dict:
    response = await client.post(
        "/tenants", json={"name": name}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    return {"X-API-Key": response.json()["api_key"]}


async def test_create_list_get_rename_delete(client):
    headers = await _tenant(client, "Acme")

    created = await client.post(
        "/categories", json={"name": "  support ", "description": "help"}, headers=headers
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "support"
    assert body["description"] == "help"
    assert set(body) == {"id", "name", "description", "created_at"}
    other = await client.post("/categories", json={"name": "billing"}, headers=headers)
    assert other.json()["description"] is None

    listed = await client.get("/categories", headers=headers)
    assert [c["name"] for c in listed.json()] == ["billing", "support"]

    got = await client.get(f"/categories/{body['id']}", headers=headers)
    assert got.status_code == 200 and got.json() == body

    renamed = await client.patch(
        f"/categories/{body['id']}", json={"name": "help desk"}, headers=headers
    )
    assert renamed.status_code == 200
    assert renamed.json()["name"] == "help desk"
    assert renamed.json()["description"] == "help"
    described = await client.patch(
        f"/categories/{body['id']}", json={"description": "new text"}, headers=headers
    )
    assert described.json()["name"] == "help desk"
    assert described.json()["description"] == "new text"

    deleted = await client.delete(f"/categories/{body['id']}", headers=headers)
    assert deleted.status_code == 204
    assert (await client.get(f"/categories/{body['id']}", headers=headers)).status_code == 404
    assert [c["name"] for c in (await client.get("/categories", headers=headers)).json()] == [
        "billing"
    ]


async def test_other_tenant_sees_nothing_and_may_reuse_names(client):
    a = await _tenant(client, "A")
    b = await _tenant(client, "B")
    created = await client.post("/categories", json={"name": "billing"}, headers=a)
    cid = created.json()["id"]

    assert (await client.get("/categories", headers=b)).json() == []
    for response in (
        await client.get(f"/categories/{cid}", headers=b),
        await client.patch(f"/categories/{cid}", json={"name": "x"}, headers=b),
        await client.delete(f"/categories/{cid}", headers=b),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["message"] == "category not found"
    assert (await client.get(f"/categories/{cid}", headers=a)).status_code == 200

    same = await client.post("/categories", json={"name": "billing"}, headers=b)
    assert same.status_code == 201


async def test_unknown_id_is_404(client):
    headers = await _tenant(client, "Acme")
    response = await client.get(f"/categories/{uuid.uuid4()}", headers=headers)
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "category not found"


async def test_duplicate_name_is_409_on_create_and_rename(client):
    headers = await _tenant(client, "Acme")
    await client.post("/categories", json={"name": "billing"}, headers=headers)
    second = await client.post("/categories", json={"name": "support"}, headers=headers)

    dup = await client.post("/categories", json={"name": " billing "}, headers=headers)
    assert dup.status_code == 409
    assert dup.json()["error"]["message"] == "category name already exists"

    rename = await client.patch(
        f"/categories/{second.json()['id']}", json={"name": "billing"}, headers=headers
    )
    assert rename.status_code == 409
    assert rename.json()["error"]["message"] == "category name already exists"
    # The session is usable after the conflict.
    assert len((await client.get("/categories", headers=headers)).json()) == 2


async def test_name_length_is_validated(client):
    headers = await _tenant(client, "Acme")
    for name in ("", "   ", "x" * 81):
        response = await client.post("/categories", json={"name": name}, headers=headers)
        assert response.status_code == 422, name
        assert response.json()["error"]["code"] == "validation_error"
    assert (
        await client.post("/categories", json={"name": "x" * 80}, headers=headers)
    ).status_code == 201
    created = await client.post("/categories", json={"name": "ok"}, headers=headers)
    patch = await client.patch(
        f"/categories/{created.json()['id']}", json={"name": "y" * 81}, headers=headers
    )
    assert patch.status_code == 422


async def test_requires_api_key(client):
    assert (await client.get("/categories")).status_code in (401, 403)


async def test_delete_leaves_conversation_uncategorized_and_lock_untouched(
    client, fake_redis, db_session
):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "user-1", "oi")
    await _drain_jobs(fake_redis)
    category = (await client.post("/categories", json={"name": "billing"}, headers=headers)).json()

    conversation = await db_session.get(Conversation, conversation_id)
    conversation.category_id = uuid.UUID(category["id"])
    conversation.category_locked = True
    await db_session.commit()

    deleted = await client.delete(f"/categories/{category['id']}", headers=headers)
    assert deleted.status_code == 204

    db_session.expire_all()
    row = (
        await db_session.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one()
    assert row.category_id is None
    assert row.category_locked is True
