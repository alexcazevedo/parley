"""`GET /conversations/{id}/messages`: one SELECT on `messages`, filtered in
SQL by `seq`, paged with `limit` and the `X-Next-Cursor` header.
"""

import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import event, select

from app.api.pagination import decode_cursor, encode_cursor
from app.models.conversation import Conversation
from app.models.message import Message, MessageDirection, MessageRole


async def _setup(client, db_session, n_text=4, tool_after=(1, 3)):
    """A conversation with `n_text` text rows after the inbound one, plus tool
    rows after the text rows at the given (0-based) positions. Returns
    (headers, conversation_id, text rows in seq order).
    """
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    agent = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "x", "model": "gpt-4o-mini"},
        headers=headers,
    )
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    sent = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel.json()["id"], "user_id": "u1", "message": "m0"},
        headers=headers,
    )
    assert sent.status_code == 201, sent.text
    conversation_id = uuid.UUID(sent.json()["conversation_id"])
    conversation = (
        await db_session.execute(select(Conversation).where(Conversation.id == conversation_id))
    ).scalar_one()

    def add(content, role, type_):
        db_session.add(
            Message(
                id=uuid.uuid4(),
                tenant_id=conversation.tenant_id,
                conversation_id=conversation_id,
                channel_id=conversation.channel_id,
                direction=MessageDirection.OUT,
                role=role,
                type=type_,
                content=content,
                created_at=datetime.now(UTC),
            )
        )

    for i in range(1, n_text):
        add(f"m{i}", MessageRole.ASSISTANT, "text")
        await db_session.flush()
        if i in tool_after:
            add(f"tool{i}", MessageRole.TOOL, "tool_result")
            await db_session.flush()
    await db_session.commit()
    return headers, conversation_id


async def _get(client, headers, conversation_id, **params):
    return await client.get(
        f"/conversations/{conversation_id}/messages", params=params, headers=headers
    )


def _contents(response):
    return [m["content"] for m in response.json()]


async def test_after_seq_returns_later_rows_with_one_statement(client, db_session, test_engine):
    headers, cid = await _setup(client, db_session, n_text=5, tool_after=())
    everything = await _get(client, headers, cid)
    seqs = [m["seq"] for m in everything.json()]
    assert _contents(everything) == ["m0", "m1", "m2", "m3", "m4"]
    assert seqs == sorted(seqs)

    statements: list[str] = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", _count)
    try:
        resp = await _get(client, headers, cid, after_seq=seqs[2])
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", _count)

    assert _contents(resp) == ["m3", "m4"]
    assert "x-next-cursor" not in resp.headers
    on_messages = [s for s in statements if "FROM messages" in s]
    assert len(on_messages) == 1
    assert "seq >" in on_messages[0]


async def test_newest_page_walks_back_to_all_rows_once(client, db_session):
    headers, cid = await _setup(client, db_session, n_text=5, tool_after=())
    seen: list[str] = []
    resp = await _get(client, headers, cid, limit=2)
    assert _contents(resp) == ["m3", "m4"]
    pages = 1
    while "x-next-cursor" in resp.headers:
        seen = _contents(resp) + seen
        resp = await _get(
            client, headers, cid, limit=2, cursor=resp.headers["x-next-cursor"]
        )
        pages += 1
    seen = _contents(resp) + seen
    assert seen == ["m0", "m1", "m2", "m3", "m4"]
    assert pages == 3


async def test_forward_paging_sets_after_seq_cursor(client, db_session):
    headers, cid = await _setup(client, db_session, n_text=5, tool_after=())
    first = await _get(client, headers, cid, after_seq=0, limit=2)
    assert _contents(first) == ["m0", "m1"]
    cursor = decode_cursor(first.headers["x-next-cursor"])
    assert cursor == {"after_seq": first.json()[-1]["seq"]}
    second = await _get(client, headers, cid, limit=2, cursor=first.headers["x-next-cursor"])
    assert _contents(second) == ["m2", "m3"]
    third = await _get(client, headers, cid, limit=2, cursor=second.headers["x-next-cursor"])
    assert _contents(third) == ["m4"]
    assert "x-next-cursor" not in third.headers


async def test_tool_rows_hidden_by_default_in_both_directions(client, db_session):
    headers, cid = await _setup(client, db_session, n_text=4, tool_after=(1, 3))
    newest = await _get(client, headers, cid)
    assert _contents(newest) == ["m0", "m1", "m2", "m3"]
    forward = await _get(client, headers, cid, after_seq=0)
    assert _contents(forward) == ["m0", "m1", "m2", "m3"]

    newest = await _get(client, headers, cid, include_tools="true")
    assert _contents(newest) == ["m0", "m1", "tool1", "m2", "m3", "tool3"]
    forward = await _get(client, headers, cid, after_seq=0, include_tools="true")
    assert _contents(forward) == ["m0", "m1", "tool1", "m2", "m3", "tool3"]


async def test_invalid_cursor_and_limit_are_422(client, db_session):
    headers, cid = await _setup(client, db_session, n_text=2, tool_after=())
    bad = await _get(client, headers, cid, cursor="garbage")
    assert bad.status_code == 422
    assert bad.json()["error"]["message"] == "invalid cursor"
    not_a_dict = await _get(client, headers, cid, cursor=encode_cursor([1]))  # type: ignore[arg-type]
    assert not_a_dict.status_code == 422
    assert (await _get(client, headers, cid, limit=501)).status_code == 422
    assert (await _get(client, headers, cid, limit=0)).status_code == 422


async def test_after_timestamp_still_works(client, db_session):
    headers, cid = await _setup(client, db_session, n_text=3, tool_after=())
    rows = (await _get(client, headers, cid)).json()
    cutoff = datetime.fromisoformat(rows[0]["created_at"]) + timedelta(microseconds=1)
    resp = await _get(client, headers, cid, after=cutoff.isoformat())
    assert _contents(resp) == ["m1", "m2"]
    future = await _get(
        client, headers, cid, after=(datetime.now(UTC) + timedelta(days=1)).isoformat()
    )
    assert future.json() == []
