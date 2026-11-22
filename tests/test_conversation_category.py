"""Moving a conversation to a category (the human wins) and listing by category."""

import os
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.api.conversations as conversations_api
from app.models.category import Category
from app.models.conversation import Conversation
from app.models.tenant import Tenant
from tests.test_operator_api import _send, _setup


async def _category(client, headers, name) -> str:
    response = await client.post("/categories", json={"name": name}, headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _move(client, headers, conversation_id, category_id):
    return await client.put(
        f"/conversations/{conversation_id}/category",
        json={"category_id": category_id},
        headers=headers,
    )


async def test_move_to_category_and_to_none(client):
    headers, channel_id, _ = await _setup(client)
    billing = await _category(client, headers, "billing")
    conv = await _send(client, headers, channel_id, "u1", "oi")

    got = (await client.get(f"/conversations/{conv}", headers=headers)).json()
    assert got["category_id"] is None and got["category_locked"] is False

    moved = await _move(client, headers, conv, billing)
    assert moved.status_code == 200, moved.text
    assert moved.json()["category_id"] == billing
    assert moved.json()["category_locked"] is True

    cleared = await _move(client, headers, conv, None)
    assert cleared.status_code == 200
    assert cleared.json()["category_id"] is None
    assert cleared.json()["category_locked"] is True


async def test_body_field_is_required(client):
    headers, channel_id, _ = await _setup(client)
    conv = await _send(client, headers, channel_id, "u1", "oi")
    response = await client.put(f"/conversations/{conv}/category", json={}, headers=headers)
    assert response.status_code == 422


async def test_unknown_conversation_and_unknown_category(client, db_session):
    headers, channel_id, _ = await _setup(client)
    billing = await _category(client, headers, "billing")
    missing = await _move(client, headers, uuid.uuid4(), billing)
    assert missing.status_code == 404
    assert missing.json()["error"]["message"] == "conversation not found"

    conv = await _send(client, headers, channel_id, "u1", "oi")
    unknown = await _move(client, headers, conv, str(uuid.uuid4()))
    assert unknown.status_code == 404
    assert unknown.json()["error"]["message"] == "category not found"
    row = await db_session.get(Conversation, conv)
    assert row.category_id is None and row.category_locked is False


async def test_other_tenants_category_is_refused_and_nothing_written(client, db_session):
    headers, channel_id, _ = await _setup(client)
    other = await client.post(
        "/tenants", json={"name": "Other"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    other_headers = {"X-API-Key": other.json()["api_key"]}
    foreign = await _category(client, other_headers, "billing")
    conv = await _send(client, headers, channel_id, "u1", "oi")

    response = await _move(client, headers, conv, foreign)
    assert response.status_code == 404
    assert response.json()["error"]["message"] == "category not found"
    row = await db_session.get(Conversation, conv)
    assert row.category_id is None and row.category_locked is False

    # And another tenant cannot move this conversation at all.
    mine = await _category(client, headers, "support")
    crossed = await _move(client, other_headers, conv, foreign)
    assert crossed.status_code == 404
    assert crossed.json()["error"]["message"] == "conversation not found"
    assert mine


async def test_move_allowed_in_every_state(client, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    billing = await _category(client, headers, "billing")
    handed = await _send(client, headers, channel_id, "u1", "oi")
    await client.post(f"/conversations/{handed}/handoff", json={}, headers=headers)
    closed = await _send(client, headers, channel_id, "u2", "oi")
    await client.post(f"/conversations/{closed}/close", headers=headers)
    for conv in (handed, closed):
        response = await _move(client, headers, conv, billing)
        assert response.status_code == 200, response.text
        assert response.json()["category_id"] == billing


async def test_list_by_category_and_uncategorized(client):
    headers, channel_id, _ = await _setup(client)
    billing = await _category(client, headers, "billing")
    support = await _category(client, headers, "support")
    a = await _send(client, headers, channel_id, "u1", "oi")
    b = await _send(client, headers, channel_id, "u2", "oi")
    c = await _send(client, headers, channel_id, "u3", "oi")
    assert (await _move(client, headers, a, billing)).status_code == 200
    assert (await _move(client, headers, b, support)).status_code == 200

    by_billing = await client.get(
        "/conversations", params={"category_id": billing}, headers=headers
    )
    assert [x["id"] for x in by_billing.json()] == [str(a)]
    assert by_billing.json()[0]["category_id"] == billing

    none = await client.get("/conversations", params={"uncategorized": "true"}, headers=headers)
    assert [x["id"] for x in none.json()] == [str(c)]

    both = await client.get(
        "/conversations", params={"category_id": billing, "uncategorized": "true"}, headers=headers
    )
    assert both.status_code == 422
    assert both.json()["error"]["message"] == "category_id and uncategorized cannot be combined"

    with_state = await client.get(
        "/conversations",
        params={"category_id": billing, "state": "CLOSED"},
        headers=headers,
    )
    assert with_state.json() == []
    with_state = await client.get(
        "/conversations",
        params={"category_id": billing, "state": "ACTIVE_AI"},
        headers=headers,
    )
    assert [x["id"] for x in with_state.json()] == [str(a)]


async def test_human_move_wins_over_a_commit_the_session_never_saw(
    client, db_session, test_engine
):
    """The session believes category NULL; the model committed `billing` behind
    its back; the human's `null` must still end up in the row, locked."""
    headers, channel_id, _ = await _setup(client)
    billing = await _category(client, headers, "billing")
    conv = await _send(client, headers, channel_id, "u1", "oi")
    row = await db_session.get(Conversation, conv)
    tenant_id = row.tenant_id
    assert row.category_id is None and row.category_locked is False
    maker = async_sessionmaker(bind=test_engine, expire_on_commit=False)

    try:
        async with maker() as other:
            await other.execute(
                update(Conversation)
                .where(Conversation.id == uuid.UUID(str(conv)))
                .values(category_id=uuid.UUID(billing))
            )
            await other.commit()

        response = await _move(client, headers, conv, None)
        assert response.status_code == 200, response.text
        assert response.json()["category_id"] is None
        assert response.json()["category_locked"] is True

        async with maker() as check:
            stored = (
                await check.execute(
                    select(Conversation.category_id, Conversation.category_locked).where(
                        Conversation.id == uuid.UUID(str(conv))
                    )
                )
            ).one()
        assert stored.category_id is None and stored.category_locked is True
    finally:
        await db_session.rollback()
        async with maker() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await session.commit()


async def test_category_deleted_mid_request_is_404_not_500(client, db_session, monkeypatch):
    headers, channel_id, _ = await _setup(client)
    conv = await _send(client, headers, channel_id, "u1", "oi")
    gone = uuid.uuid4()

    class StaleRepo:
        """The existence check still sees the category; the commit will not."""

        def __init__(self, session) -> None:
            pass

        async def get(self, tenant_id, category_id):
            return Category(id=gone)

    monkeypatch.setattr(conversations_api, "CategoriesRepository", StaleRepo)

    response = await _move(client, headers, conv, str(gone))
    assert response.status_code == 404, response.text
    assert response.json()["error"]["message"] == "category not found"
    row = await db_session.get(Conversation, conv)
    await db_session.refresh(row)
    assert row.category_id is None and row.category_locked is False
