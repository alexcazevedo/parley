"""The Evolution webhook answers fast (PRODUCT.md E4.4).

A repeated delivery is recognised before any other work; images and audio are
downloaded by the worker right before the turn that needs them, not by the
webhook; the stored payload carries no base64.
"""

import base64
import copy
import json
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select
from structlog.testing import capture_logs

import app.api.webhooks as webhooks_module
from app.agents.runtime import AgentRuntime
from app.channels.base import IncomingMedia
from app.channels.evolution import strip_base64
from app.config import get_settings
from app.conversations.turn import UNSUPPORTED_MEDIA_REPLY
from app.llm.base import ROLE_USER, ImagePart, LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.media.fetch import fetch_pending_media
from app.messages.ingest import MediaRejected, validate_media
from app.models.channel import Channel
from app.models.message import Message, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.workers.main import run_once
from app.workers.scheduler import tick
from tests.test_evolution import _auth, _create_evolution_channel, _create_tenant

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://evolution.example.com"
GET_BASE64_URL = f"{BASE}/chat/getBase64FromMediaMessage/harness-instance"
SEND_TEXT_URL = f"{BASE}/message/sendText/harness-instance"
STT_URL = "https://api.openai.com/v1/audio/transcriptions"
APIKEY = {"apikey": "evo-secret-xyz"}
DEBOUNCE_SECONDS = 0.2
TRANSCRIPT = "quero saber do pedido 12345"
JID = "5511999998888"


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def _fixture(name: str) -> dict:
    return copy.deepcopy(json.loads((FIXTURES / name).read_text()))


def _png() -> bytes:
    return (FIXTURES / "order_label.png").read_bytes()


def _mp3() -> bytes:
    return (FIXTURES / "voice_note.mp3").read_bytes()


def _media_response(data: bytes, mime: str) -> httpx.Response:
    return httpx.Response(
        200, json={"base64": base64.b64encode(data).decode("ascii"), "mimetype": mime}
    )


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


async def _rows(db_session, channel_id: str) -> list[Message]:
    db_session.expire_all()
    result = await db_session.execute(
        select(Message).where(Message.channel_id == uuid.UUID(channel_id)).order_by(Message.seq)
    )
    return list(result.scalars().all())


def _user_contents(provider: FakeLLMProvider) -> list:
    return [m.content for m in provider.calls[0]["messages"] if m.role == ROLE_USER]


# --- repeated delivery ----------------------------------------------------------


@respx.mock
async def test_repeated_media_delivery_makes_no_download_one_message_one_job(
    client, fake_redis, db_session, media_root
):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    route = respx.post(GET_BASE64_URL).mock(return_value=_media_response(_png(), "image/png"))

    payload = _fixture("evolution_image_upsert.json")
    first = await client.post(f"/webhooks/whatsapp/{channel_id}", json=payload, headers=APIKEY)
    second = await client.post(f"/webhooks/whatsapp/{channel_id}", json=payload, headers=APIKEY)

    assert first.status_code == 200 and first.json() == {"status": "ok"}
    assert second.status_code == 200 and second.json() == {"status": "ok"}
    assert route.call_count == 0
    assert len(await _rows(db_session, channel_id)) == 1
    assert await fake_redis.llen(JOBS_KEY) == 1


async def test_repeated_delivery_is_recognised_before_the_rate_limit(
    client, fake_redis, db_session, monkeypatch
):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    real_limit = webhooks_module.inbound_over_limit
    calls: list[int] = []

    async def counting_limit(*args, **kwargs):
        calls.append(1)
        return await real_limit(*args, **kwargs)

    monkeypatch.setattr(webhooks_module, "inbound_over_limit", counting_limit)
    payload = _fixture("evolution_messages_upsert.json")
    await client.post(f"/webhooks/whatsapp/{channel_id}", json=payload, headers=APIKEY)
    second = await client.post(f"/webhooks/whatsapp/{channel_id}", json=payload, headers=APIKEY)

    assert second.json() == {"status": "ok"}
    assert len(calls) == 1
    assert len(await _rows(db_session, channel_id)) == 1
    assert await fake_redis.llen(JOBS_KEY) == 1


# --- the webhook downloads nothing; the turn does ------------------------------


@respx.mock
async def test_image_webhook_answers_before_download_and_turn_sees_the_image(
    client, fake_redis, db_session, media_root
):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    route = respx.post(GET_BASE64_URL).mock(return_value=_media_response(_png(), "image/jpeg"))
    respx.post(SEND_TEXT_URL).mock(return_value=httpx.Response(200, json={}))

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_fixture("evolution_image_upsert.json"),
        headers=APIKEY,
    )
    assert response.json() == {"status": "ok"}
    assert route.call_count == 0
    (row,) = await _rows(db_session, channel_id)
    assert row.type == "image"
    assert row.media_key is None

    await _drain_jobs(fake_redis)
    provider = FakeLLMProvider([LLMResponse(content="Vi a etiqueta.")])
    await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    assert route.call_count == 1
    body = json.loads(route.calls.last.request.content)
    assert body["message"]["key"]["id"] == "3EB0C767D26A1D5F9E1B"
    (parts,) = _user_contents(provider)
    images = [p for p in parts if isinstance(p, ImagePart)]
    assert [(i.mime, i.data) for i in images] == [("image/png", _png())]

    rows = await _rows(db_session, channel_id)
    assert rows[0].media_mime == "image/png"
    assert rows[0].media_key.startswith(f"{tenant['id']}/")
    assert rows[-1].role == MessageRole.ASSISTANT
    assert rows[-1].content == "Vi a etiqueta."


@respx.mock
async def test_audio_webhook_downloads_in_turn_and_model_gets_transcript(
    client, fake_redis, db_session, media_root
):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    route = respx.post(GET_BASE64_URL).mock(
        return_value=_media_response(_mp3(), "audio/ogg; codecs=opus")
    )
    respx.post(SEND_TEXT_URL).mock(return_value=httpx.Response(200, json={}))
    stt = respx.post(STT_URL).mock(return_value=httpx.Response(200, json={"text": TRANSCRIPT}))

    await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_fixture("evolution_audio_upsert.json"),
        headers=APIKEY,
    )
    assert route.call_count == 0

    await _drain_jobs(fake_redis)
    provider = FakeLLMProvider([LLMResponse(content="Chega amanhã.")])
    await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    assert route.call_count == 1
    assert stt.called
    assert _user_contents(provider) == [TRANSCRIPT]
    rows = await _rows(db_session, channel_id)
    assert rows[0].media_mime == "audio/mpeg"
    assert rows[0].content == TRANSCRIPT


@pytest.mark.parametrize(
    "media_response",
    [
        httpx.Response(500, json={"error": "boom"}),
        # Downloaded, but the bytes are not an image: rejected by validation.
        _media_response(b"definitely not an image", "image/jpeg"),
    ],
    ids=["failed", "rejected"],
)
async def test_failed_or_rejected_download_gets_unsupported_media_reply(
    client, fake_redis, db_session, media_root, media_response
):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    with respx.mock(assert_all_called=False) as router:
        route = router.post(GET_BASE64_URL).mock(return_value=media_response)
        send = router.post(SEND_TEXT_URL).mock(return_value=httpx.Response(200, json={}))

        await client.post(
            f"/webhooks/whatsapp/{channel_id}",
            json=_fixture("evolution_image_upsert.json"),
            headers=APIKEY,
        )
        await _drain_jobs(fake_redis)
        provider = FakeLLMProvider([])
        with capture_logs() as logs:
            await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    assert route.called
    assert provider.call_count == 0
    assert json.loads(send.calls.last.request.content)["text"] == UNSUPPORTED_MEDIA_REPLY
    rows = await _rows(db_session, channel_id)
    assert rows[0].type == "unsupported"
    assert rows[0].content == "[mídia não recebida]"
    assert rows[0].media_key is None
    assert [p for p in media_root.rglob("*") if p.is_file()] == []

    failed = [e for e in logs if e["event"] == "media.fetch_failed"]
    assert len(failed) == 1
    assert failed[0]["message_id"] == str(rows[0].id)
    dumped = json.dumps(failed, default=str)
    assert JID not in dumped
    assert "3EB0C767D26A1D5F9E1B" not in dumped


# --- stored payload ---------------------------------------------------------------


async def test_stored_payload_has_no_base64_or_thumbnail(
    client, fake_redis, db_session, media_root
):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    payload = _fixture("evolution_image_upsert.json")
    payload["data"]["message"]["imageMessage"]["jpegThumbnail"] = "AAAA"
    payload["data"]["message"]["base64"] = "BBBB"
    payload["data"]["contextInfo"] = [{"quoted": {"base64": "CCCC"}}]

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}", json=payload, headers=APIKEY
    )
    assert response.json() == {"status": "ok"}

    (row,) = await _rows(db_session, channel_id)
    stored = json.dumps(row.raw_payload)
    assert '"base64"' not in stored
    assert '"jpegThumbnail"' not in stored
    assert row.raw_payload["_media"] == {"kind": "image", "mimetype": "image/jpeg"}
    assert row.raw_payload["data"]["message"]["imageMessage"]["caption"] == "olha isso"


def test_strip_base64_removes_keys_at_any_depth_and_returns_a_copy():
    payload = {
        "base64": "x",
        "keep": 1,
        "data": {
            "message": {"imageMessage": {"jpegThumbnail": "y", "caption": "c"}},
            "list": [{"base64": "z", "ok": True}, "plain"],
        },
        "_media": {"kind": "image", "mimetype": "image/jpeg"},
    }
    original = copy.deepcopy(payload)

    stripped = strip_base64(payload)

    assert stripped == {
        "keep": 1,
        "data": {
            "message": {"imageMessage": {"caption": "c"}},
            "list": [{"ok": True}, "plain"],
        },
        "_media": {"kind": "image", "mimetype": "image/jpeg"},
    }
    assert payload == original


# --- validate_media ---------------------------------------------------------------


def test_validate_media_returns_the_sniffed_mime(media_root):
    assert validate_media("image", IncomingMedia(data=_png(), mime="image/jpeg")) == "image/png"
    assert (
        validate_media("audio", IncomingMedia(data=_mp3(), mime="audio/ogg; codecs=opus"))
        == "audio/mpeg"
    )


@pytest.mark.parametrize(
    ("message_type", "media"),
    [
        ("image", IncomingMedia(data=b"x", mime="application/pdf")),
        ("image", IncomingMedia(data=b"not an image at all", mime="image/png")),
        ("audio", IncomingMedia(data=_png(), mime="audio/ogg")),
    ],
)
def test_validate_media_rejects(message_type, media, media_root):
    with pytest.raises(MediaRejected):
        validate_media(message_type, media)


def test_validate_media_rejects_oversize(monkeypatch):
    monkeypatch.setenv("MEDIA_MAX_BYTES", "10")
    get_settings.cache_clear()
    try:
        with pytest.raises(MediaRejected):
            validate_media("image", IncomingMedia(data=_png(), mime="image/png"))
    finally:
        get_settings.cache_clear()


# --- fetch_pending_media ----------------------------------------------------------


async def _webhook_image_row(client, db_session) -> tuple[Message, Channel, dict]:
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    with respx.mock(assert_all_called=False):
        await client.post(
            f"/webhooks/whatsapp/{channel_id}",
            json=_fixture("evolution_image_upsert.json"),
            headers=APIKEY,
        )
    (row,) = await _rows(db_session, channel_id)
    channel = await db_session.get(Channel, uuid.UUID(channel_id))
    return row, channel, tenant


async def test_fetch_pending_media_skips_rows_that_already_have_media(
    client, fake_redis, db_session, media_root
):
    row, channel, _ = await _webhook_image_row(client, db_session)
    row.media_key = f"{row.tenant_id}/already.png"
    row.media_mime = "image/png"
    await db_session.commit()

    with respx.mock(assert_all_called=False) as router:
        route = router.post(GET_BASE64_URL).mock(
            return_value=_media_response(_png(), "image/png")
        )
        await fetch_pending_media(db_session, [row], channel)

    assert route.call_count == 0
    assert row.media_key == f"{row.tenant_id}/already.png"
    assert row.type == "image"


async def test_fetch_pending_media_ignores_non_evolution_channels(
    client, fake_redis, db_session, media_root
):
    row, channel, _ = await _webhook_image_row(client, db_session)
    webchat = Channel(
        id=uuid.uuid4(),
        tenant_id=channel.tenant_id,
        type="webchat",
        name="Web",
        agent_id=channel.agent_id,
        config={},
    )

    with respx.mock(assert_all_called=False) as router:
        route = router.route().mock(return_value=httpx.Response(200, json={}))
        await fetch_pending_media(db_session, [row], webchat)

    assert route.call_count == 0
    assert row.type == "image"
    assert row.media_key is None


async def test_fetch_pending_media_refused_endpoint_makes_no_call(
    client, fake_redis, db_session, media_root
):
    row, channel, _ = await _webhook_image_row(client, db_session)
    channel.config = {"base_url": "http://10.0.0.5:8080", "instance": "harness-instance"}
    await db_session.commit()

    with respx.mock(assert_all_called=False) as router:
        route = router.route().mock(return_value=_media_response(_png(), "image/png"))
        await fetch_pending_media(db_session, [row], channel)

    assert route.call_count == 0
    assert row.type == "unsupported"
    assert row.content == "[mídia não recebida]"
    assert row.media_key is None
