"""External channel tracer: a project forwards a customer message, the turn
runs in-process, and the agent's reply is read back from the channel's event
feed (`GET /channels/{id}/events`).

The model is scripted (`FakeLLMProvider`); the external adapter sends nothing.
"""

import os
import uuid

from sqlalchemy import func, select

from app.agents.runtime import AgentRuntime
from app.conversations import turn as turn_module
from app.conversations.turn import process_turn
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.channel_event import ChannelEvent
from app.models.message import Message, MessageDirection, MessageRole

REPLY = "Olá! Seu pedido sai hoje."


async def _tenant(client) -> dict:
    response = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    return {"X-API-Key": response.json()["api_key"]}


async def _agent(client, headers) -> str:
    response = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Você é o atendente da Acme.", "model": "gpt-4o-mini"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _channel(client, headers, agent_id, channel_type="external", config=None):
    return await client.post(
        "/channels",
        json={"type": channel_type, "name": "Inbox", "agent_id": agent_id, "config": config or {}},
        headers=headers,
    )


async def _setup(client, channel_type="external", config=None) -> tuple[dict, str, dict]:
    headers = await _tenant(client)
    agent_id = await _agent(client, headers)
    response = await _channel(client, headers, agent_id, channel_type, config)
    assert response.status_code == 201, response.text
    return headers, response.json()["id"], response.json()


async def _post(client, headers, channel_id, text, message_id=None, path="external"):
    body = {"channel_id": channel_id, "user_id": "cust-1", "message": text}
    if message_id is not None:
        body["message_id"] = message_id
    return await client.post(f"/channels/{path}/messages", json=body, headers=headers)


async def _inbound(db_session, conversation_id: uuid.UUID) -> list[Message]:
    result = await db_session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.IN,
        )
        .order_by(Message.seq)
    )
    return list(result.scalars().all())


async def _run_turn(db_session, conversation_id: uuid.UUID, reply: str = REPLY):
    messages = await _inbound(db_session, conversation_id)
    runtime = AgentRuntime(provider=FakeLLMProvider([LLMResponse(content=reply)]))
    return await process_turn(db_session, conversation_id, messages, runtime)


async def _events(client, headers, channel_id, **params):
    return await client.get(f"/channels/{channel_id}/events", params=params, headers=headers)


async def test_reply_appears_once_in_the_feed(client, db_session):
    headers, channel_id, created = await _setup(client)
    assert isinstance(created["events_secret"], str) and len(created["events_secret"]) >= 32

    response = await _post(client, headers, channel_id, "cadê meu pedido?")
    assert response.status_code == 201, response.text
    assert response.json()["duplicate"] is False
    conversation_id = uuid.UUID(response.json()["conversation_id"])

    reply = await _run_turn(db_session, conversation_id)
    assert isinstance(reply, Message) and reply.content == REPLY

    feed = await _events(client, headers, channel_id)
    assert feed.status_code == 200, feed.text
    events = feed.json()
    assert len(events) == 1
    event = events[0]
    assert set(event) == {"id", "seq", "type", "conversation_id", "created_at", "data"}
    assert event["type"] == "message.reply"
    assert event["conversation_id"] == str(conversation_id)
    assert event["data"] == {"message_id": str(reply.id), "seq": reply.seq, "content": REPLY}

    after = await _events(client, headers, channel_id, after=event["seq"])
    assert after.status_code == 200
    assert after.json() == []


async def test_humanized_reply_records_one_event_per_bubble(client, db_session, monkeypatch):
    headers, channel_id, _ = await _setup(client, config={"humanize": True})

    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(turn_module.asyncio, "sleep", no_sleep)
    response = await _post(client, headers, channel_id, "oi")
    conversation_id = uuid.UUID(response.json()["conversation_id"])
    parts = ["Oi! Achei seu pedido.", "Ele sai hoje.", "Mais alguma coisa?"]
    await _run_turn(db_session, conversation_id, reply="\n\n".join(parts))

    events = (await _events(client, headers, channel_id)).json()
    assert [e["data"]["content"] for e in events] == parts
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs)
    page = (await _events(client, headers, channel_id, after=seqs[0], limit=1)).json()
    assert [e["seq"] for e in page] == [seqs[1]]


async def test_webchat_conversation_records_no_events(client, db_session):
    headers, channel_id, created = await _setup(client, channel_type="webchat")
    assert created["events_secret"] is None
    response = await _post(client, headers, channel_id, "oi", path="webchat")
    conversation_id = uuid.UUID(response.json()["conversation_id"])

    reply = await _run_turn(db_session, conversation_id)
    assert isinstance(reply, Message)

    count = await db_session.scalar(
        select(func.count())
        .select_from(ChannelEvent)
        .where(ChannelEvent.conversation_id == conversation_id)
    )
    assert count == 0
    feed = await _events(client, headers, channel_id)
    assert feed.status_code == 422
    assert "not an external channel" in feed.text


async def test_events_secret_is_shown_only_on_create(client, db_session):
    headers, channel_id, created = await _setup(client)
    assert created["events_secret"]

    one = await client.get(f"/channels/{channel_id}", headers=headers)
    assert one.status_code == 200
    assert "events_secret" not in one.json()
    listed = await client.get("/channels", headers=headers)
    assert listed.status_code == 200
    assert all("events_secret" not in channel for channel in listed.json())
    assert created["events_secret"] not in one.text + listed.text


async def test_same_message_id_twice_stores_one_message(client, db_session):
    headers, channel_id, _ = await _setup(client)
    first = await _post(client, headers, channel_id, "oi", message_id="ext-1")
    second = await _post(client, headers, channel_id, "oi", message_id="ext-1")
    assert first.status_code == 201 and second.status_code == 201
    assert second.json()["duplicate"] is True
    assert second.json()["message_id"] == first.json()["message_id"]

    conversation_id = uuid.UUID(first.json()["conversation_id"])
    assert len(await _inbound(db_session, conversation_id)) == 1


async def test_posting_to_a_webchat_channel_is_refused(client, db_session):
    headers, channel_id, _ = await _setup(client, channel_type="webchat")
    response = await _post(client, headers, channel_id, "oi")
    assert response.status_code == 422
    assert "channel is not an external channel" in response.text


async def test_another_tenants_feed_is_not_found(client, db_session):
    _, channel_id, _ = await _setup(client)
    other = await _tenant(client)
    feed = await _events(client, other, channel_id)
    assert feed.status_code == 404
    posted = await _post(client, other, channel_id, "oi")
    assert posted.status_code == 404


async def test_feed_limit_is_bounded(client, db_session):
    headers, channel_id, _ = await _setup(client)
    assert (await _events(client, headers, channel_id, limit=501)).status_code == 422
    assert (await _events(client, headers, channel_id, limit=500)).status_code == 200


async def test_private_events_url_is_refused(client, db_session):
    headers = await _tenant(client)
    agent_id = await _agent(client, headers)
    refused = await _channel(
        client, headers, agent_id, config={"events_url": "http://127.0.0.1:9000/events"}
    )
    assert refused.status_code == 422
    assert "invalid events_url" in refused.text
    accepted = await _channel(
        client, headers, agent_id, config={"events_url": "https://inbox.example.com/events"}
    )
    assert accepted.status_code == 201, accepted.text


async def test_failure_notice_records_a_reply_event(client, db_session):
    headers, channel_id, _ = await _setup(client)
    response = await _post(client, headers, channel_id, "oi")
    conversation_id = uuid.UUID(response.json()["conversation_id"])

    assert await turn_module.send_failure_notice(db_session, conversation_id) is True
    events = (await _events(client, headers, channel_id)).json()
    assert [e["type"] for e in events] == ["message.reply"]
    stored = await db_session.scalar(
        select(Message).where(
            Message.conversation_id == conversation_id, Message.role == MessageRole.ASSISTANT
        )
    )
    assert events[0]["data"]["message_id"] == str(stored.id)


async def test_migration_0018_adds_the_enum_value_and_the_feed_table():
    import asyncpg

    from tests.conftest import _ASYNCPG_ADMIN_DSN
    from tests.test_migration_open_conversation import _alembic

    db_name = f"parley_mig_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/postgres")
    await admin.execute(f'CREATE DATABASE "{db_name}"')
    database_url = (
        _ASYNCPG_ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://") + f"/{db_name}"
    )
    try:
        _alembic(database_url, "upgrade", "0018")
        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            labels = await conn.fetch(
                "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = 'channel_type'"
            )
            assert "external" in {r["enumlabel"] for r in labels}
            indexes = await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'channel_events'"
            )
            assert {"ix_channel_events_channel_id_seq", "channel_events_seq_key"} <= {
                r["indexname"] for r in indexes
            }
        finally:
            await conn.close()
        _alembic(database_url, "downgrade", "0017")
        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            assert await conn.fetchval("SELECT to_regclass('channel_events')") is None
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await admin.close()
