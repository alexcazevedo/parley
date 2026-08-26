"""Evolution (WhatsApp) inbound media: image/audio payloads fetch their
bytes from Evolution's `getBase64FromMediaMessage` endpoint and land in the
same ingest pipeline as webchat uploads; other media kinds become
`unsupported` messages instead of being dropped.

`media_root` points `MEDIA_ROOT` at a per-test temp dir (see
`tests/test_media_ingest.py`) so nothing lands under the repo's default
`./media` while running the suite.
"""

import base64
import copy
import json
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from app.channels.evolution import EvolutionAdapter, IgnoredEvent
from app.config import get_settings
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


async def _first_message(client, headers) -> dict:
    conversations = await client.get("/conversations", headers=headers)
    conversation_id = conversations.json()[0]["id"]
    messages = await client.get(f"/conversations/{conversation_id}/messages", headers=headers)
    return messages.json()[0]


@respx.mock
async def test_image_payload_stores_media_and_enqueues_job(client, fake_redis, media_root):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    png_bytes = (FIXTURES / "order_label.png").read_bytes()
    respx.post(GET_BASE64_URL).mock(
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

    message = await _first_message(client, headers)
    assert message["type"] == "image"
    assert message["content"] == "olha isso"
    assert message["media_mime"] == "image/jpeg"

    assert await fake_redis.llen(JOBS_KEY) == 1


@respx.mock
async def test_audio_payload_normalizes_mime_and_stores_media(client, fake_redis, media_root):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    mp3_bytes = (FIXTURES / "voice_note.mp3").read_bytes()
    respx.post(GET_BASE64_URL).mock(
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

    message = await _first_message(client, headers)
    assert message["type"] == "audio"
    assert message["content"] == ""
    assert message["media_mime"] == "audio/ogg"

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
async def test_media_fetch_failure_falls_back_to_unsupported(client, fake_redis, media_root):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    respx.post(GET_BASE64_URL).mock(return_value=httpx.Response(500, json={"error": "boom"}))

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_fixture("evolution_image_upsert.json"),
        headers={"apikey": "evo-secret-xyz"},
    )
    assert response.status_code == 200

    message = await _first_message(client, headers)
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
