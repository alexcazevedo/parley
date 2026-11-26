"""Per-agent texts for a technical error and for unsupported media.

Null on the agent means the built-in default; the three use sites (a failed
run, a dead-letter notice, an all-unsupported batch) read the agent's text.
"""

import uuid

from sqlalchemy import select

from app.agents.context import IMAGE_PLACEHOLDER, render_message_text
from app.agents.runtime import ERROR_MESSAGE, AgentRuntime
from app.config import get_settings
from app.conversations.turn import (
    TURN_FAILED_MESSAGE,
    UNSUPPORTED_MEDIA_REPLY,
    send_failure_notice,
)
from app.models.message import Message, MessageDirection, MessageRole
from tests.test_agent_loop import (
    ExplodingProvider,
    _drain_jobs,
    _messages,
    _run_turn,
    _send,
    _setup,
)

SORRY = "Sorry, try again."
NO_VIDEO = "I only read text."


# --- API -----------------------------------------------------------------------


async def test_api_round_trip_and_null_clears(client):
    headers, _, agent_id = await _setup(
        client, error_message=SORRY, unsupported_media_message=NO_VIDEO
    )
    got = await client.get(f"/agents/{agent_id}", headers=headers)
    assert got.json()["error_message"] == SORRY
    assert got.json()["unsupported_media_message"] == NO_VIDEO

    # Not mentioned: untouched.
    patched = await client.patch(f"/agents/{agent_id}", json={"name": "Bot 2"}, headers=headers)
    assert patched.json()["error_message"] == SORRY

    patched = await client.patch(
        f"/agents/{agent_id}", json={"error_message": None}, headers=headers
    )
    assert patched.status_code == 200
    assert patched.json()["error_message"] is None
    assert patched.json()["unsupported_media_message"] == NO_VIDEO


async def test_api_defaults_are_null_and_empty_is_422(client):
    headers, _, agent_id = await _setup(client)
    got = await client.get(f"/agents/{agent_id}", headers=headers)
    assert got.json()["error_message"] is None
    assert got.json()["unsupported_media_message"] is None
    for field in ("error_message", "unsupported_media_message"):
        for value in ("", "x" * 1001):
            response = await client.patch(
                f"/agents/{agent_id}", json={field: value}, headers=headers
            )
            assert response.status_code == 422


async def test_clearing_does_not_change_other_null_handling(client):
    headers, _, agent_id = await _setup(client)
    patched = await client.patch(
        f"/agents/{agent_id}", json={"handoff_message": None}, headers=headers
    )
    assert patched.status_code == 200
    assert patched.json()["handoff_message"]


# --- use sites -----------------------------------------------------------------


async def _replies(db_session, conversation_id: uuid.UUID) -> list[str]:
    return [
        m.content for m in await _messages(db_session, conversation_id)
        if m.role == MessageRole.ASSISTANT
    ]


async def test_failed_run_replies_with_agent_text(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client, error_message=SORRY)
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    await _run_turn(fake_redis, db_session, AgentRuntime(provider=ExplodingProvider()))

    assert await _replies(db_session, conversation_id) == [SORRY]


async def test_failed_run_without_text_uses_default(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    await _run_turn(fake_redis, db_session, AgentRuntime(provider=ExplodingProvider()))

    assert await _replies(db_session, conversation_id) == [ERROR_MESSAGE]


async def test_dead_letter_notice_uses_agent_text(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client, error_message=SORRY)
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    assert await send_failure_notice(db_session, conversation_id) is True

    assert await _replies(db_session, conversation_id) == [SORRY]


async def test_dead_letter_notice_without_text_uses_default(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)

    assert await send_failure_notice(db_session, conversation_id) is True

    assert await _replies(db_session, conversation_id) == [TURN_FAILED_MESSAGE]


async def _unsupported_turn(client, fake_redis, db_session, **agent):
    headers, channel_id, _ = await _setup(client, **agent)
    conversation_id = await _send(client, headers, channel_id, "oi")
    await _drain_jobs(fake_redis)
    rows = await db_session.execute(
        select(Message).where(
            Message.conversation_id == conversation_id, Message.direction == MessageDirection.IN
        )
    )
    for message in rows.scalars():
        message.type = "unsupported"
    await db_session.commit()
    await _run_turn(fake_redis, db_session, AgentRuntime(provider=ExplodingProvider()))
    return await _replies(db_session, conversation_id)


async def test_unsupported_media_uses_agent_text(client, fake_redis, db_session):
    replies = await _unsupported_turn(
        client, fake_redis, db_session, unsupported_media_message=NO_VIDEO
    )
    assert replies == [NO_VIDEO]


async def test_unsupported_media_without_text_uses_default(client, fake_redis, db_session):
    assert await _unsupported_turn(client, fake_redis, db_session) == [UNSUPPORTED_MEDIA_REPLY]


# --- placeholders --------------------------------------------------------------


def _image(content: str = "") -> Message:
    return Message(type="image", content=content)


def test_placeholders_default_and_follow_settings(monkeypatch):
    assert render_message_text(_image()) == IMAGE_PLACEHOLDER
    settings = get_settings()
    monkeypatch.setattr(settings, "image_placeholder", "[image]")
    monkeypatch.setattr(settings, "audio_placeholder", "[voice]")
    assert render_message_text(_image()) == "[image]"
    assert render_message_text(_image("look")) == "[image]: look"
    assert render_message_text(Message(type="audio", content="")) == "[voice]"
