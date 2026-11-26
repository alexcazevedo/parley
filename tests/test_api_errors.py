"""E4.9/E4.10: webchat routes reject other channel types with 422, and request
validation failures use the API's error envelope without echoing input."""

import os
import uuid

from sqlalchemy import func, select

from app.models.message import Message
from app.queue.redis_queue import JOBS_KEY

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
WRONG_TYPE = "channel is not a webchat channel"


async def _tenant(client, name="Acme") -> dict:
    r = await client.post(
        "/tenants", json={"name": name}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    return {"X-API-Key": r.json()["api_key"]}


async def _evolution_channel(client, headers) -> str:
    agent = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Hi.", "model": "gpt-4o-mini"},
        headers=headers,
    )
    channel = await client.post(
        "/channels",
        json={
            "type": "whatsapp_evolution",
            "name": "WhatsApp",
            "agent_id": agent.json()["id"],
            "config": {"base_url": "https://evolution.example.com", "instance": "i1"},
            "secret": "evo-secret-xyz",
        },
        headers=headers,
    )
    assert channel.status_code == 201, channel.text
    return channel.json()["id"]


async def _assert_nothing_stored(db_session, fake_redis, channel_id) -> None:
    query = (
        select(func.count())
        .select_from(Message)
        .where(Message.channel_id == uuid.UUID(channel_id))
    )
    count = (await db_session.execute(query)).scalar_one()
    assert count == 0
    assert await fake_redis.llen(JOBS_KEY) == 0


def _assert_wrong_type(response) -> None:
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["message"] == WRONG_TYPE
    assert error["request_id"]


async def test_webchat_messages_rejects_evolution_channel(client, db_session, fake_redis):
    headers = await _tenant(client)
    channel_id = await _evolution_channel(client, headers)
    r = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "u", "message": "hi"},
        headers=headers,
    )
    _assert_wrong_type(r)
    await _assert_nothing_stored(db_session, fake_redis, channel_id)


async def test_webchat_media_rejects_evolution_channel(client, db_session, fake_redis):
    headers = await _tenant(client)
    channel_id = await _evolution_channel(client, headers)
    r = await client.post(
        "/channels/webchat/media",
        data={"channel_id": channel_id, "user_id": "u"},
        files={"file": ("a.png", PNG, "image/png")},
        headers=headers,
    )
    _assert_wrong_type(r)
    await _assert_nothing_stored(db_session, fake_redis, channel_id)


async def test_dev_messages_rejects_evolution_channel(client, db_session, fake_redis):
    headers = await _tenant(client)
    channel_id = await _evolution_channel(client, headers)
    r = await client.post(
        "/dev/messages",
        json={"channel_id": channel_id, "user_id": "u", "message": "hi"},
        headers=headers,
    )
    _assert_wrong_type(r)
    await _assert_nothing_stored(db_session, fake_redis, channel_id)


async def test_other_tenants_channel_is_404_not_422(client):
    owner = await _tenant(client, "Owner")
    other = await _tenant(client, "Other")
    channel_id = await _evolution_channel(client, owner)
    r = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "u", "message": "hi"},
        headers=other,
    )
    assert r.status_code == 404
    assert r.json()["error"]["message"] == "channel not found"


async def test_validation_error_uses_envelope_without_echoing_input(client):
    headers = await _tenant(client)
    r = await client.post(
        "/channels",
        json={"type": "webchat", "agent_id": "secret-api-key-123"},
        headers=headers,
    )
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["request_id"]
    fields = [d["field"] for d in body["error"]["details"]]
    assert "name" in fields
    assert "secret-api-key-123" not in r.text
    assert "input" not in r.text
