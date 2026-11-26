"""Evolution (WhatsApp) inbound media: image/audio rows are stored by the
webhook without media, and the worker (`fetch_pending_media`) fetches their
bytes from Evolution's `getBase64FromMediaMessage` endpoint, validated as
webchat uploads are; other media kinds become
`unsupported` messages instead of being dropped.

`media_root` points `MEDIA_ROOT` at a per-test temp dir (see
`tests/test_media_ingest.py`) so nothing lands under the repo's default
`./media` while running the suite.
"""

import asyncio
import base64
import copy
import gzip
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import select

import app.channels.evolution as evolution_module
from app.channels.evolution import EvolutionAdapter, IgnoredEvent, MediaTooLarge
from app.config import get_settings
from app.media.fetch import fetch_pending_media
from app.models.channel import Channel
from app.models.message import Message
from app.queue.redis_queue import JOBS_KEY
from tests.test_evolution import _auth, _create_evolution_channel, _create_tenant

FIXTURES = Path(__file__).parent / "fixtures"
GET_BASE64_URL = (
    "https://evolution.example.com/chat/getBase64FromMediaMessage/harness-instance"
)


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def _get_message(db_session, message_id: str) -> Message:
    result = await db_session.execute(select(Message).where(Message.id == message_id))
    return result.scalar_one()


async def _fetch_after_webhook(client, headers, db_session, route) -> dict:
    """The webhook stored the row without downloading (E4.4); run the worker's
    download (`fetch_pending_media`, as the turn does) and return the row."""
    assert route.call_count == 0
    message = await _first_message(client, headers)
    assert message["media_mime"] is None
    row = await _get_message(db_session, message["id"])
    channel = await db_session.get(Channel, row.channel_id)
    await fetch_pending_media(db_session, [row], channel)
    await db_session.commit()
    return await _first_message(client, headers)


async def _first_message(client, headers) -> dict:
    conversations = await client.get("/conversations", headers=headers)
    conversation_id = conversations.json()[0]["id"]
    messages = await client.get(f"/conversations/{conversation_id}/messages", headers=headers)
    return messages.json()[0]


@respx.mock
async def test_image_payload_stores_media_and_enqueues_job(
    client, fake_redis, db_session, media_root
):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    png_bytes = (FIXTURES / "order_label.png").read_bytes()
    route = respx.post(GET_BASE64_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "base64": base64.b64encode(png_bytes).decode("ascii"),
                "mimetype": "image/jpeg",
            },
        )
    )

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_fixture("evolution_image_upsert.json"),
        headers={"apikey": "evo-secret-xyz"},
    )
    assert response.status_code == 200

    message = await _fetch_after_webhook(client, headers, db_session, route)
    assert route.call_count == 1
    assert message["type"] == "image"
    assert message["content"] == "olha isso"
    assert message["media_mime"] == "image/png"  # sniffed, not the declared jpeg

    assert await fake_redis.llen(JOBS_KEY) == 1


@respx.mock
async def test_audio_payload_normalizes_mime_and_stores_media(
    client, fake_redis, db_session, media_root
):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    mp3_bytes = (FIXTURES / "voice_note.mp3").read_bytes()
    route = respx.post(GET_BASE64_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "base64": base64.b64encode(mp3_bytes).decode("ascii"),
                "mimetype": "audio/ogg; codecs=opus",
            },
        )
    )

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_fixture("evolution_audio_upsert.json"),
        headers={"apikey": "evo-secret-xyz"},
    )
    assert response.status_code == 200

    message = await _fetch_after_webhook(client, headers, db_session, route)
    assert route.call_count == 1
    assert message["type"] == "audio"
    assert message["content"] == ""
    assert message["media_mime"] == "audio/mpeg"  # sniffed, not the declared ogg

    assert await fake_redis.llen(JOBS_KEY) == 1


@respx.mock
async def test_video_payload_is_unsupported_no_media_call(client, fake_redis, media_root):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    route = respx.post(GET_BASE64_URL).mock(return_value=httpx.Response(200, json={}))

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_fixture("evolution_video_upsert.json"),
        headers={"apikey": "evo-secret-xyz"},
    )
    assert response.status_code == 200
    assert not route.called

    message = await _first_message(client, headers)
    assert message["type"] == "unsupported"
    assert message["content"] == "[vídeo]"
    assert message["media_mime"] is None

    assert await fake_redis.llen(JOBS_KEY) == 1


@respx.mock
async def test_media_fetch_failure_falls_back_to_unsupported(
    client, fake_redis, db_session, media_root
):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    route = respx.post(GET_BASE64_URL).mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_fixture("evolution_image_upsert.json"),
        headers={"apikey": "evo-secret-xyz"},
    )
    assert response.status_code == 200

    message = await _fetch_after_webhook(client, headers, db_session, route)
    assert message["type"] == "unsupported"
    assert message["content"] == "[mídia não recebida]"

    assert await fake_redis.llen(JOBS_KEY) == 1


@pytest.mark.parametrize(
    ("kind", "expected_content"),
    [
        ("videoMessage", "[vídeo]"),
        ("documentMessage", "[documento]"),
        ("stickerMessage", "[figurinha]"),
        ("locationMessage", "[localização]"),
        ("contactMessage", "[contato]"),
    ],
)
def test_parse_incoming_maps_unsupported_kinds(kind, expected_content):
    payload = copy.deepcopy(_fixture("evolution_image_upsert.json"))
    payload["tenant_id"] = "00000000-0000-0000-0000-000000000001"
    payload["channel_id"] = "00000000-0000-0000-0000-000000000002"
    payload["data"]["message"] = {kind: {}}

    adapter = EvolutionAdapter()
    incoming = adapter.parse_incoming(payload)

    assert incoming.type == "unsupported"
    assert incoming.content == expected_content
    assert incoming.media is None


def test_parse_incoming_ignores_reaction_message():
    payload = copy.deepcopy(_fixture("evolution_image_upsert.json"))
    payload["tenant_id"] = "00000000-0000-0000-0000-000000000001"
    payload["channel_id"] = "00000000-0000-0000-0000-000000000002"
    payload["data"]["message"] = {
        "reactionMessage": {"text": "👍", "key": {"id": "abc"}}
    }

    adapter = EvolutionAdapter()
    with pytest.raises(IgnoredEvent):
        adapter.parse_incoming(payload)


def test_parse_incoming_maps_image_message():
    payload = copy.deepcopy(_fixture("evolution_image_upsert.json"))
    payload["tenant_id"] = "00000000-0000-0000-0000-000000000001"
    payload["channel_id"] = "00000000-0000-0000-0000-000000000002"

    adapter = EvolutionAdapter()
    incoming = adapter.parse_incoming(payload)

    assert incoming.type == "image"
    assert incoming.content == "olha isso"
    assert incoming.media is None
    assert incoming.raw["_media"] == {"kind": "image", "mimetype": "image/jpeg"}


def test_parse_incoming_maps_audio_message():
    payload = copy.deepcopy(_fixture("evolution_audio_upsert.json"))
    payload["tenant_id"] = "00000000-0000-0000-0000-000000000001"
    payload["channel_id"] = "00000000-0000-0000-0000-000000000002"

    adapter = EvolutionAdapter()
    incoming = adapter.parse_incoming(payload)

    assert incoming.type == "audio"
    assert incoming.content == ""
    assert incoming.media is None
    assert incoming.raw["_media"] == {"kind": "audio", "mimetype": "audio/ogg; codecs=opus"}


# --- F2: capped media download -------------------------------------------------

_CHUNK = b"A" * 10_000


def _counting_body(total_chunks: int, pulled: list[int]):
    async def gen():
        for _ in range(total_chunks):
            pulled.append(1)
            yield _CHUNK

    return gen()


@pytest.fixture
def small_media_cap(monkeypatch):
    monkeypatch.setenv("MEDIA_MAX_BYTES", "1000")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _adapter_inputs():
    payload = copy.deepcopy(_fixture("evolution_image_upsert.json"))
    payload["tenant_id"] = "00000000-0000-0000-0000-000000000001"
    payload["channel_id"] = "00000000-0000-0000-0000-000000000002"
    incoming = EvolutionAdapter().parse_incoming(payload)
    channel = SimpleNamespace(
        id="c",
        tenant_id="t",
        secret_encrypted=None,
        config={"base_url": "https://evolution.example.com", "instance": "harness-instance"},
    )
    return incoming, channel


@respx.mock
async def test_fetch_media_stops_reading_past_cap(small_media_cap):
    pulled: list[int] = []
    total_chunks = 100  # 1 MB served; raw cap is ~66 KB
    respx.post(GET_BASE64_URL).mock(
        return_value=httpx.Response(200, content=_counting_body(total_chunks, pulled))
    )
    incoming, channel = _adapter_inputs()

    with pytest.raises(MediaTooLarge):
        await EvolutionAdapter().fetch_media(incoming.external_message_id, "image/jpeg", channel)

    assert len(pulled) < total_chunks


@respx.mock
async def test_fetch_media_rejects_decoded_data_over_cap(small_media_cap):
    # Fits the raw cap (66 KB) but decodes to more than MEDIA_MAX_BYTES (1000).
    big = base64.b64encode(b"x" * 5000).decode("ascii")
    respx.post(GET_BASE64_URL).mock(return_value=httpx.Response(200, json={"base64": big}))
    incoming, channel = _adapter_inputs()

    with pytest.raises(MediaTooLarge):
        await EvolutionAdapter().fetch_media(incoming.external_message_id, "image/jpeg", channel)


@respx.mock
async def test_fetch_media_rejects_compressed_response(small_media_cap):
    body = gzip.compress(json.dumps({"base64": "AAAA"}).encode())
    respx.post(GET_BASE64_URL).mock(
        return_value=httpx.Response(
            200, content=body, headers={"Content-Encoding": "gzip"}
        )
    )
    incoming, channel = _adapter_inputs()

    with pytest.raises(MediaTooLarge):
        await EvolutionAdapter().fetch_media(incoming.external_message_id, "image/jpeg", channel)


@respx.mock
async def test_oversized_media_through_webhook_is_unsupported(
    client, fake_redis, db_session, media_root, small_media_cap
):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    pulled: list[int] = []
    route = respx.post(GET_BASE64_URL).mock(
        return_value=httpx.Response(200, content=_counting_body(100, pulled))
    )

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_fixture("evolution_image_upsert.json"),
        headers={"apikey": "evo-secret-xyz"},
    )
    assert response.status_code == 200

    message = await _fetch_after_webhook(client, headers, db_session, route)
    assert len(pulled) < 100
    assert message["type"] == "unsupported"
    assert message["content"] == "[mídia não recebida]"
    assert message["media_mime"] is None
    assert [p for p in media_root.rglob("*") if p.is_file()] == []


# --- total download deadline ---------------------------------------------------


def _never_finishing_body():
    async def gen():
        yield _CHUNK
        await asyncio.sleep(60)  # a server dripping/stalling; cancelled by the deadline
        yield _CHUNK

    return gen()


@respx.mock
async def test_fetch_media_times_out_on_stalled_download(monkeypatch):
    monkeypatch.setattr(evolution_module, "MEDIA_DOWNLOAD_DEADLINE_SECONDS", 0.2)
    respx.post(GET_BASE64_URL).mock(
        return_value=httpx.Response(200, content=_never_finishing_body())
    )
    incoming, channel = _adapter_inputs()

    with pytest.raises(TimeoutError):
        await EvolutionAdapter().fetch_media(incoming.external_message_id, "image/jpeg", channel)


def test_deadline_constant_is_30_seconds():
    assert evolution_module.MEDIA_DOWNLOAD_DEADLINE_SECONDS == 30.0


@respx.mock
async def test_stalled_media_through_webhook_is_unsupported(
    client, fake_redis, db_session, media_root, monkeypatch
):
    monkeypatch.setattr(evolution_module, "MEDIA_DOWNLOAD_DEADLINE_SECONDS", 0.2)
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)
    route = respx.post(GET_BASE64_URL).mock(
        return_value=httpx.Response(200, content=_never_finishing_body())
    )

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_fixture("evolution_image_upsert.json"),
        headers={"apikey": "evo-secret-xyz"},
    )
    assert response.status_code == 200

    message = await _fetch_after_webhook(client, headers, db_session, route)
    assert message["type"] == "unsupported"
    assert message["content"] == "[mídia não recebida]"
    assert [p for p in media_root.rglob("*") if p.is_file()] == []
