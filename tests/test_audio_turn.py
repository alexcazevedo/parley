"""The agent hears voice notes: upload -> debounce -> turn -> transcript.

Same end-to-end pipeline as `tests/test_vision_turn.py`, with the voice note
coming in through `POST /channels/webchat/media` and STT mocked with respx on
`.../audio/transcriptions`, so what is asserted is what a real turn would
have transcribed and sent to the model.
"""

import os
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from app.agents.runtime import AgentRuntime
from app.config import get_settings
from app.llm.base import ROLE_USER, LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.media.prepare import AUDIO_FAILED_TEXT, prepare_media_messages
from app.models.agent_run import AgentRun
from app.models.message import Message, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.workers.main import run_once
from app.workers.scheduler import tick

FIXTURES = Path(__file__).parent / "fixtures"
DEBOUNCE_SECONDS = 0.2
TRANSCRIPT = "esse é meu pedido 12345, quando chega?"


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    """Uploads land in a temp dir instead of the repo's `./media`."""
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _mp3_bytes() -> bytes:
    return (FIXTURES / "voice_note.mp3").read_bytes()


async def _setup(client) -> tuple[dict, str, uuid.UUID]:
    tenant_response = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    tenant = tenant_response.json()
    headers = {"X-API-Key": tenant["api_key"]}
    agent = await client.post(
        "/agents",
        json={
            "name": "Bot",
            "instructions": "Você é o atendente da Acme.",
            "model": "gpt-4o-mini",
        },
        headers=headers,
    )
    assert agent.status_code == 201, agent.text
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    return headers, channel.json()["id"], uuid.UUID(tenant["id"])


async def _upload_audio(client, headers, channel_id):
    response = await client.post(
        "/channels/webchat/media",
        data={"channel_id": str(channel_id), "user_id": "user-1"},
        files={"file": ("voice_note.mp3", _mp3_bytes(), "audio/mpeg")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _send_text(client, headers, channel_id, text: str):
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "user-1", "message": text},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def _drain_jobs(fake_redis) -> None:
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None


async def _run_turn(fake_redis, db_session, runtime) -> None:
    await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)


async def _messages(db_session, conversation_id) -> list[Message]:
    result = await db_session.execute(
        select(Message)
        .where(Message.conversation_id == uuid.UUID(str(conversation_id)))
        .order_by(Message.seq)
    )
    return list(result.scalars().all())


async def _get_message(db_session, message_id: str) -> Message:
    result = await db_session.execute(select(Message).where(Message.id == uuid.UUID(message_id)))
    return result.scalar_one()


def _user_contents(provider: FakeLLMProvider, call_index: int = 0) -> list:
    return [m.content for m in provider.calls[call_index]["messages"] if m.role == ROLE_USER]


def _stt_route(router, *, base_url="https://api.openai.com/v1"):
    return router.post(f"{base_url}/audio/transcriptions")


async def test_transcribed_audio_reaches_the_model_as_text(
    client, fake_redis, db_session, media_root
):
    headers, channel_id, _ = await _setup(client)
    upload = await _upload_audio(client, headers, channel_id)
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider([LLMResponse(content="Seu pedido chega amanhã.")])
    async with respx.mock(assert_all_called=False) as router:
        route = _stt_route(router).mock(
            return_value=httpx.Response(200, json={"text": TRANSCRIPT})
        )
        await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    assert route.called
    assert provider.call_count == 1
    assert _user_contents(provider) == [TRANSCRIPT]

    message = await _get_message(db_session, upload["message_id"])
    assert message.content == TRANSCRIPT
    assert message.raw_payload["transcript_status"] == "ok"

    messages = await _messages(db_session, upload["conversation_id"])
    assert messages[-1].role == MessageRole.ASSISTANT
    assert messages[-1].content == "Seu pedido chega amanhã."


async def test_stt_failure_degrades_to_placeholder_and_turn_still_replies(
    client, fake_redis, db_session, media_root
):
    headers, channel_id, _ = await _setup(client)
    upload = await _upload_audio(client, headers, channel_id)
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider([LLMResponse(content="Pode repetir, por favor?")])
    async with respx.mock(assert_all_called=False) as router:
        _stt_route(router).mock(return_value=httpx.Response(500, text="boom"))
        await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    assert provider.call_count == 1
    assert _user_contents(provider) == [AUDIO_FAILED_TEXT]

    message = await _get_message(db_session, upload["message_id"])
    assert message.content == AUDIO_FAILED_TEXT
    assert message.raw_payload["transcript_status"] == "failed"

    messages = await _messages(db_session, upload["conversation_id"])
    assert messages[-1].content == "Pode repetir, por favor?"


async def test_stt_timeout_degrades_to_placeholder(client, fake_redis, db_session, media_root):
    headers, channel_id, _ = await _setup(client)
    upload = await _upload_audio(client, headers, channel_id)
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider([LLMResponse(content="Pode repetir, por favor?")])
    async with respx.mock(assert_all_called=False) as router:
        _stt_route(router).mock(side_effect=httpx.ReadTimeout("timed out"))
        await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    assert provider.call_count == 1
    assert _user_contents(provider) == [AUDIO_FAILED_TEXT]

    message = await _get_message(db_session, upload["message_id"])
    assert message.content == AUDIO_FAILED_TEXT
    assert message.raw_payload["transcript_status"] == "failed"


async def test_prepare_media_messages_is_idempotent(client, db_session, media_root):
    headers, channel_id, _ = await _setup(client)
    upload = await _upload_audio(client, headers, channel_id)
    message = await _get_message(db_session, upload["message_id"])

    async with respx.mock(assert_all_called=False) as router:
        route = _stt_route(router).mock(
            return_value=httpx.Response(200, json={"text": TRANSCRIPT})
        )
        await prepare_media_messages(db_session, [message])
        await prepare_media_messages(db_session, [message])

    assert route.call_count == 1
    assert message.content == TRANSCRIPT


async def test_stt_base_url_and_api_key_overrides_are_used(
    client, fake_redis, db_session, media_root, monkeypatch
):
    monkeypatch.setenv("STT_BASE_URL", "https://stt.example.test/v1")
    monkeypatch.setenv("STT_API_KEY", "stt-secret-key")
    get_settings.cache_clear()

    headers, channel_id, _ = await _setup(client)
    upload = await _upload_audio(client, headers, channel_id)
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider([LLMResponse(content="ok")])
    async with respx.mock(assert_all_called=False) as router:
        route = _stt_route(router, base_url="https://stt.example.test/v1").mock(
            return_value=httpx.Response(200, json={"text": TRANSCRIPT})
        )
        await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    assert route.called
    request = route.calls.last.request
    assert str(request.url) == "https://stt.example.test/v1/audio/transcriptions"
    assert request.headers["authorization"] == "Bearer stt-secret-key"

    message = await _get_message(db_session, upload["message_id"])
    assert message.content == TRANSCRIPT
    get_settings.cache_clear()


async def test_mixed_batch_preserves_arrival_order_in_input_text(
    client, fake_redis, db_session, media_root
):
    headers, channel_id, _ = await _setup(client)
    await _send_text(client, headers, channel_id, "oi, tudo bem?")
    upload = await _upload_audio(client, headers, channel_id)
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider([LLMResponse(content="Tudo sim, como posso ajudar?")])
    async with respx.mock(assert_all_called=False) as router:
        _stt_route(router).mock(return_value=httpx.Response(200, json={"text": TRANSCRIPT}))
        await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    messages = await _messages(db_session, upload["conversation_id"])
    assert messages[-1].content == "Tudo sim, como posso ajudar?"

    # `AgentRun.input_text` is the exact joined `input_text` the turn built:
    # text first, then the audio transcript, in arrival order.
    conversation_id = uuid.UUID(str(upload["conversation_id"]))
    result = await db_session.execute(
        select(AgentRun).where(AgentRun.conversation_id == conversation_id)
    )
    agent_run = result.scalar_one()
    assert agent_run.input_text == f"oi, tudo bem?\n{TRANSCRIPT}"
