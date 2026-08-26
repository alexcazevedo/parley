"""The agent sees images: upload -> debounce -> turn -> multimodal prompt.

Same end-to-end pipeline as `tests/test_agent_loop.py` (ingest, drain, tick)
with the photo coming in through `POST /channels/webchat/media`, so what is
asserted is what a real turn would have sent to the model. The LLM is scripted
(`FakeLLMProvider`), which records `content` verbatim — including the
`ImagePart`/`TextPart` list a vision turn produces.
"""

import base64
import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.agents.context import IMAGE_PLACEHOLDER
from app.agents.runtime import AgentRuntime
from app.channels.base import IncomingMessage
from app.config import get_settings
from app.conversations.turn import UNSUPPORTED_MEDIA_REPLY
from app.llm.base import ROLE_USER, ImagePart, LLMMessage, LLMResponse, TextPart
from app.llm.providers.fake import FakeLLMProvider
from app.llm.providers.openai_compatible import _message_to_wire
from app.messages.ingest import ingest
from app.models.message import Message, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.workers.main import run_once
from app.workers.scheduler import tick

FIXTURES = Path(__file__).parent / "fixtures"
DEBOUNCE_SECONDS = 0.2
CAPTION = "o que está escrito aqui?"


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    """Uploads land in a temp dir instead of the repo's `./media`."""
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _png_bytes() -> bytes:
    return (FIXTURES / "order_label.png").read_bytes()


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


async def _upload_image(client, headers, channel_id, caption: str | None = None):
    response = await client.post(
        "/channels/webchat/media",
        data={
            "channel_id": str(channel_id),
            "user_id": "user-1",
            **({"caption": caption} if caption is not None else {}),
        },
        files={"file": ("order_label.png", _png_bytes(), "image/png")},
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


async def _ingest_unsupported(db_session, fake_redis, tenant_id, channel_id) -> uuid.UUID:
    """A video/sticker-style message: persisted with its type, no media stored."""
    incoming = IncomingMessage(
        tenant_id=tenant_id,
        channel_id=uuid.UUID(str(channel_id)),
        external_user_id="user-1",
        external_message_id=str(uuid.uuid4()),
        type="unsupported",
        content="[vídeo recebido]",
        timestamp=datetime.now(UTC),
        raw={},
    )
    result = await ingest(db_session, RedisQueue(fake_redis), incoming)
    await db_session.commit()
    return result.conversation_id


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
        .order_by(Message.created_at)
    )
    return list(result.scalars().all())


def _user_contents(provider: FakeLLMProvider, call_index: int = 0) -> list:
    return [m.content for m in provider.calls[call_index]["messages"] if m.role == ROLE_USER]


async def test_image_message_reaches_the_model_as_an_image_part(
    client, fake_redis, db_session, media_root
):
    headers, channel_id, _ = await _setup(client)
    upload = await _upload_image(client, headers, channel_id, caption=CAPTION)
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider([LLMResponse(content="Está escrito 12345.")])
    await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    assert provider.call_count == 1
    contents = _user_contents(provider)
    assert len(contents) == 1
    parts = contents[0]
    assert isinstance(parts, list)
    images = [p for p in parts if isinstance(p, ImagePart)]
    texts = [p.text for p in parts if isinstance(p, TextPart)]
    assert len(images) == 1
    assert images[0].mime == "image/png"
    assert images[0].data == _png_bytes()
    assert texts == [CAPTION]

    messages = await _messages(db_session, upload["conversation_id"])
    assert messages[-1].role == MessageRole.ASSISTANT
    assert messages[-1].content == "Está escrito 12345."


async def test_image_without_caption_carries_the_placeholder_as_its_text(
    client, fake_redis, db_session, media_root
):
    headers, channel_id, _ = await _setup(client)
    await _upload_image(client, headers, channel_id)
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider([LLMResponse(content="Recebi sua foto.")])
    await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    parts = _user_contents(provider)[0]
    assert [p.text for p in parts if isinstance(p, TextPart)] == [IMAGE_PLACEHOLDER]


async def test_non_vision_deployment_degrades_the_image_to_text(
    client, fake_redis, db_session, media_root, monkeypatch
):
    headers, channel_id, _ = await _setup(client)
    await _upload_image(client, headers, channel_id, caption=CAPTION)
    await _drain_jobs(fake_redis)

    monkeypatch.setenv("LLM_VISION_ENABLED", "false")
    get_settings.cache_clear()

    provider = FakeLLMProvider([LLMResponse(content="Pode descrever a foto?")])
    await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    contents = _user_contents(provider)
    assert contents == [f"{IMAGE_PLACEHOLDER}: {CAPTION}"]
    for message in provider.calls[0]["messages"]:
        assert not isinstance(message.content, list)


async def test_unreadable_media_file_degrades_but_still_answers(
    client, fake_redis, db_session, media_root
):
    headers, channel_id, _ = await _setup(client)
    upload = await _upload_image(client, headers, channel_id, caption=CAPTION)
    await _drain_jobs(fake_redis)

    # The row still points at a key whose file is gone: the loader raises.
    message = (await _messages(db_session, upload["conversation_id"]))[0]
    (media_root / message.media_key).unlink()

    provider = FakeLLMProvider([LLMResponse(content="Não consegui ver a imagem.")])
    await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    assert provider.call_count == 1
    assert _user_contents(provider) == [f"{IMAGE_PLACEHOLDER}: {CAPTION}"]

    messages = await _messages(db_session, upload["conversation_id"])
    assert messages[-1].content == "Não consegui ver a imagem."


async def test_only_the_newest_images_carry_bytes(client, fake_redis, db_session, media_root):
    headers, channel_id, _ = await _setup(client)
    for index in range(4):
        await _upload_image(client, headers, channel_id, caption=f"foto {index}")
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider([LLMResponse(content="Vi as fotos.")])
    await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    contents = _user_contents(provider)
    assert len(contents) == 4
    # settings.vision_max_images defaults to 3: the oldest photo is text only.
    assert contents[0] == f"{IMAGE_PLACEHOLDER}: foto 0"
    for index, parts in enumerate(contents[1:], start=1):
        assert isinstance(parts, list)
        assert [p.data for p in parts if isinstance(p, ImagePart)] == [_png_bytes()]
        assert [p.text for p in parts if isinstance(p, TextPart)] == [f"foto {index}"]


async def test_unsupported_only_batch_replies_without_calling_the_model(
    client, fake_redis, db_session, media_root
):
    headers, channel_id, tenant_id = await _setup(client)
    conversation_id = await _ingest_unsupported(db_session, fake_redis, tenant_id, channel_id)
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider([])
    await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    assert provider.call_count == 0
    messages = await _messages(db_session, conversation_id)
    assert messages[-1].role == MessageRole.ASSISTANT
    assert messages[-1].content == UNSUPPORTED_MEDIA_REPLY


async def test_mixed_batch_with_unsupported_media_still_runs_the_agent(
    client, fake_redis, db_session, media_root
):
    headers, channel_id, tenant_id = await _setup(client)
    await _ingest_unsupported(db_session, fake_redis, tenant_id, channel_id)
    sent = await _send_text(client, headers, channel_id, "e aí, chegou?")
    await _drain_jobs(fake_redis)

    provider = FakeLLMProvider([LLMResponse(content="Chegou sim!")])
    await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    assert provider.call_count == 1
    messages = await _messages(db_session, sent["conversation_id"])
    assert messages[-1].content == "Chegou sim!"


def test_multimodal_content_serializes_to_openai_parts():
    raw = b"\x89PNG\r\n\x1a\nfake"
    wire = _message_to_wire(
        LLMMessage(
            role=ROLE_USER,
            content=[ImagePart(mime="image/png", data=raw), TextPart(text=CAPTION)],
        )
    )

    assert wire["role"] == ROLE_USER
    image_part, text_part = wire["content"]
    assert text_part == {"type": "text", "text": CAPTION}
    assert image_part["type"] == "image_url"
    url = image_part["image_url"]["url"]
    prefix = "data:image/png;base64,"
    assert url.startswith(prefix)
    assert base64.b64decode(url[len(prefix) :]) == raw

    # Plain text content is untouched.
    assert _message_to_wire(LLMMessage(role=ROLE_USER, content="oi"))["content"] == "oi"
