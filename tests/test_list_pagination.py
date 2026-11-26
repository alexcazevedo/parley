"""Keyset paging on the six list endpoints: `limit`, `cursor`, `X-Next-Cursor`."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from app.models.conversation import Conversation, ConversationState


async def _tenant(client, name="Acme"):
    response = await client.post(
        "/tenants", json={"name": name}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    return {"X-API-Key": response.json()["api_key"]}


async def _agent(client, headers, name="Bot"):
    r = await client.post(
        "/agents",
        json={"name": name, "instructions": "x", "model": "gpt-4o-mini"},
        headers=headers,
    )
    return r.json()["id"]


async def _channel(client, headers, agent_id, name="Web"):
    r = await client.post(
        "/channels",
        json={"type": "webchat", "name": name, "agent_id": agent_id},
        headers=headers,
    )
    return r.json()["id"]


async def _seed(client, headers, kind, n=5):
    """Create `n` rows of `kind` (names z..a-ish so name order differs from creation order)."""
    if kind == "agents":
        for i in range(n):
            await _agent(client, headers, f"agent-{n - i}")
    elif kind == "tools":
        for i in range(n):
            r = await client.post(
                "/tools",
                json={"name": f"tool-{i}", "endpoint": "https://example.com/x"},
                headers=headers,
            )
            assert r.status_code == 201, r.text
    elif kind == "channels":
        agent_id = await _agent(client, headers)
        for i in range(n):
            await _channel(client, headers, agent_id, f"ch-{i}")
    elif kind == "contacts":
        channel_id = await _channel(client, headers, await _agent(client, headers))
        for i in range(n):
            r = await client.post(
                "/contacts",
                json={"channel_id": channel_id, "external_user_id": f"u{i}"},
                headers=headers,
            )
            assert r.status_code == 201, r.text
    else:
        for i in range(n):
            r = await client.post("/categories", json={"name": f"cat-{n - i}"}, headers=headers)
            assert r.status_code == 201, r.text


async def _walk(client, path, headers, limit, **params):
    pages, cursor = [], None
    while True:
        query = {"limit": limit, **params}
        if cursor:
            query["cursor"] = cursor
        r = await client.get(path, params=query, headers=headers)
        assert r.status_code == 200, r.text
        pages.append(r.json())
        cursor = r.headers.get("X-Next-Cursor")
        if cursor is None:
            return pages
        assert len(pages) < 20


@pytest.mark.parametrize("kind", ["agents", "tools", "channels", "contacts", "categories"])
async def test_walk_simple_lists(client, kind):
    headers = await _tenant(client)
    other = await _tenant(client, "Other")
    await _seed(client, headers, kind)
    await _seed(client, other, kind, n=3)

    full = (await client.get(f"/{kind}", headers=headers)).json()
    assert len(full) >= 5

    pages = await _walk(client, f"/{kind}", headers, 2)
    assert [len(p) for p in pages] == [2, 2, 1]
    flat = [row["id"] for page in pages for row in page]
    assert flat == [row["id"] for row in full]
    assert len(set(flat)) == len(flat)
    if kind == "categories":
        names = [row["name"] for page in pages for row in page]
        assert names == sorted(names)


async def test_paging_sizes_exact(client):
    headers = await _tenant(client)
    await _seed(client, headers, "agents")
    pages = await _walk(client, "/agents", headers, 2)
    assert [len(p) for p in pages] == [2, 2, 1]
    last = await client.get("/agents", params={"limit": 5}, headers=headers)
    assert "X-Next-Cursor" not in last.headers


async def test_default_limit_and_validation(client):
    headers = await _tenant(client)
    await _seed(client, headers, "categories", n=3)
    assert len((await client.get("/categories", headers=headers)).json()) == 3
    for path in ("agents", "tools", "channels", "contacts", "categories", "conversations"):
        for bad_limit in (0, 501):
            r = await client.get(f"/{path}", params={"limit": bad_limit}, headers=headers)
            assert r.status_code == 422, (path, bad_limit)
        bad = await client.get(f"/{path}", params={"cursor": "garbage"}, headers=headers)
        assert bad.status_code == 422, path
        assert bad.json()["error"]["message"] == "invalid cursor"


async def test_default_returns_at_most_100(client):
    headers = await _tenant(client)
    for i in range(101):
        await client.post("/categories", json={"name": f"c{i:03}"}, headers=headers)
    r = await client.get("/categories", headers=headers)
    assert len(r.json()) == 100
    assert "X-Next-Cursor" in r.headers


async def test_cursor_missing_keys_or_bad_values(client):
    from app.api.pagination import encode_cursor

    headers = await _tenant(client)
    for path, values in (
        ("agents", {"id": "x"}),
        ("agents", {"created_at": "nope", "id": "x"}),
        ("agents", {"created_at": "2024-01-01T00:00:00", "id": str(uuid.uuid4())}),
        ("categories", {"name": "a"}),
        ("conversations", {"created_at": "2024-01-01T00:00:00+00:00"}),
    ):
        r = await client.get(f"/{path}", params={"cursor": encode_cursor(values)}, headers=headers)
        assert r.status_code == 422, (path, values)


async def test_walk_conversations(client, db_session):
    headers = await _tenant(client)
    other = await _tenant(client, "Other")
    channel_id = await _channel(client, headers, await _agent(client, headers))
    other_channel = await _channel(client, other, await _agent(client, other))
    ids = []
    for i in range(7):
        r = await client.post(
            "/channels/webchat/messages",
            json={"channel_id": channel_id, "user_id": f"u{i}", "message": "hi"},
            headers=headers,
        )
        assert r.status_code == 201, r.text
        ids.append(r.json()["conversation_id"])
    await client.post(
        "/channels/webchat/messages",
        json={"channel_id": other_channel, "user_id": "z", "message": "hi"},
        headers=other,
    )

    now = datetime.now(UTC)
    # Mixed: ties on last_message_at, some nulls, some states differ.
    plan = [now, now, now - timedelta(hours=1), None, None, None, now]
    for cid, last in zip(ids, plan, strict=True):
        await db_session.execute(
            update(Conversation).where(Conversation.id == cid).values(last_message_at=last)
        )
    for cid in ids[:3]:
        await db_session.execute(
            update(Conversation)
            .where(Conversation.id == cid)
            .values(state=ConversationState.HUMAN_HANDOFF)
        )
    await db_session.commit()

    full = (await client.get("/conversations", headers=headers)).json()
    assert sorted(c["id"] for c in full) == sorted(ids)
    nulls_start = next(i for i, c in enumerate(full) if c["last_message_at"] is None)
    assert all(c["last_message_at"] is None for c in full[nulls_start:])

    for limit in (1, 2, 3):
        pages = await _walk(client, "/conversations", headers, limit)
        assert [c["id"] for p in pages for c in p] == [c["id"] for c in full]

    pages = await _walk(client, "/conversations", headers, 2, state="HUMAN_HANDOFF")
    flat = [c["id"] for p in pages for c in p]
    assert sorted(flat) == sorted(ids[:3])
    assert all(c["state"] == "HUMAN_HANDOFF" for p in pages for c in p)
