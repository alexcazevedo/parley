"""PRODUCT.md success demo (E5), one test per clause, through the API."""

import os
import uuid

from sqlalchemy import select

from app.models.conversation import Conversation
from app.models.message import Message, MessageDirection, MessageRole


async def _tenant(client):
    response = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    return {"X-API-Key": response.json()["api_key"]}


def _agent_body(name="Bot", **extra):
    return {"name": name, "instructions": "x", "model": "gpt-4o-mini", **extra}


async def test_agent_fields_round_trip(client):
    headers = await _tenant(client)
    created = await client.post(
        "/agents",
        json=_agent_body(
            sentiment_escalation_enabled=True, handoff_message="Um momento, por favor."
        ),
        headers=headers,
    )
    assert created.status_code == 201, created.text
    read = await client.get(f"/agents/{created.json()['id']}", headers=headers)
    assert read.status_code == 200
    assert read.json()["sentiment_escalation_enabled"] is True
    assert read.json()["handoff_message"] == "Um momento, por favor."


async def test_temperature_50_is_422(client):
    headers = await _tenant(client)
    response = await client.post("/agents", json=_agent_body(temperature=50), headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


async def test_tool_named_remember_is_422(client):
    headers = await _tenant(client)
    response = await client.post(
        "/tools",
        json={"name": "remember", "endpoint": "https://example.com/x"},
        headers=headers,
    )
    assert response.status_code == 422
    assert "reserved" in str(response.json()["error"]["details"])


async def test_five_agents_walked_two_at_a_time(client):
    headers = await _tenant(client)
    created = []
    for i in range(5):
        r = await client.post("/agents", json=_agent_body(f"agent-{i}"), headers=headers)
        created.append(r.json()["id"])

    seen, pages, cursor = [], 0, None
    while True:
        params = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        r = await client.get("/agents", params=params, headers=headers)
        assert r.status_code == 200
        pages += 1
        seen += [a["id"] for a in r.json()]
        cursor = r.headers.get("X-Next-Cursor")
        if cursor is None:
            break
    assert pages == 3
    assert sorted(seen) == sorted(created)
    assert len(set(seen)) == 5


async def test_poll_with_after_seq_returns_only_later_messages(client, db_session):
    headers = await _tenant(client)
    agent = await client.post("/agents", json=_agent_body(), headers=headers)
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    sent = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel.json()["id"], "user_id": "u1", "message": "first"},
        headers=headers,
    )
    assert sent.status_code == 201, sent.text
    cid = uuid.UUID(sent.json()["conversation_id"])

    before = (await client.get(f"/conversations/{cid}/messages", headers=headers)).json()
    last_seq = before[-1]["seq"]

    conversation = (
        await db_session.execute(select(Conversation).where(Conversation.id == cid))
    ).scalar_one()
    for text in ("reply one", "reply two"):
        db_session.add(
            Message(
                id=uuid.uuid4(),
                tenant_id=conversation.tenant_id,
                conversation_id=cid,
                channel_id=conversation.channel_id,
                direction=MessageDirection.OUT,
                role=MessageRole.ASSISTANT,
                type="text",
                content=text,
            )
        )
        await db_session.flush()
    await db_session.commit()

    polled = await client.get(
        f"/conversations/{cid}/messages", params={"after_seq": last_seq}, headers=headers
    )
    assert polled.status_code == 200
    assert [m["content"] for m in polled.json()] == ["reply one", "reply two"]
