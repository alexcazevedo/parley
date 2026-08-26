"""Media ingest tests: webchat upload -> stored file -> image/audio message
-> served back tenant-scoped.

`media_root` points `MEDIA_ROOT` at a per-test temp dir so nothing lands
under the repo's default `./media` while running the suite.
"""

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.message import Message
from tests.test_ingest import _auth, _create_channel, _create_tenant

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def _upload_media(
    client,
    headers,
    channel_id,
    user_id,
    filename,
    content,
    content_type,
    caption=None,
    message_id=None,
):
    data = {"channel_id": str(channel_id), "user_id": user_id}
    if caption is not None:
        data["caption"] = caption
    if message_id is not None:
        data["message_id"] = message_id
    files = {"file": (filename, content, content_type)}
    return await client.post(
        "/channels/webchat/media", data=data, files=files, headers=headers
    )


async def _get_message(db_session, message_id: str) -> Message:
    result = await db_session.execute(
        select(Message).where(Message.id == uuid.UUID(message_id))
    )
    return result.scalar_one()


async def test_upload_image_creates_image_message(client, db_session, media_root):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_channel(client, headers)
    png_bytes = (FIXTURES / "order_label.png").read_bytes()

    response = await _upload_media(
        client, headers, channel_id, "user-1", "order_label.png", png_bytes, "image/png"
    )

    assert response.status_code == 201
    body = response.json()
    assert body["duplicate"] is False

    message = await _get_message(db_session, body["message_id"])
    assert message.type == "image"
    assert message.media_mime == "image/png"
    assert message.content == ""
    assert message.media_key is not None

    stored_path = media_root / message.media_key
    assert stored_path.is_file()
    assert stored_path.read_bytes() == png_bytes


async def test_upload_image_with_caption_sets_content(client, db_session, media_root):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_channel(client, headers)
    png_bytes = (FIXTURES / "order_label.png").read_bytes()

    response = await _upload_media(
        client,
        headers,
        channel_id,
        "user-1",
        "order_label.png",
        png_bytes,
        "image/png",
        caption="esse é meu pedido",
    )

    assert response.status_code == 201
    message = await _get_message(db_session, response.json()["message_id"])
    assert message.content == "esse é meu pedido"


async def test_upload_audio_creates_audio_message(client, db_session, media_root):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_channel(client, headers)
    mp3_bytes = (FIXTURES / "voice_note.mp3").read_bytes()

    response = await _upload_media(
        client, headers, channel_id, "user-1", "voice_note.mp3", mp3_bytes, "audio/mpeg"
    )

    assert response.status_code == 201
    message = await _get_message(db_session, response.json()["message_id"])
    assert message.type == "audio"
    assert message.media_mime == "audio/mpeg"
    assert message.content == ""


async def test_upload_unsupported_content_type_is_rejected(client, media_root):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_channel(client, headers)

    response = await _upload_media(
        client, headers, channel_id, "user-1", "note.txt", b"hello", "text/plain"
    )

    assert response.status_code == 415


async def test_oversize_media_is_rejected(client, media_root, monkeypatch):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_channel(client, headers)

    monkeypatch.setenv("MEDIA_MAX_BYTES", "10")
    get_settings.cache_clear()

    png_bytes = (FIXTURES / "order_label.png").read_bytes()
    response = await _upload_media(
        client, headers, channel_id, "user-1", "order_label.png", png_bytes, "image/png"
    )

    assert response.status_code == 413


async def test_get_media_returns_bytes_and_is_tenant_scoped(client, media_root):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_channel(client, headers)
    png_bytes = (FIXTURES / "order_label.png").read_bytes()

    upload = await _upload_media(
        client, headers, channel_id, "user-1", "order_label.png", png_bytes, "image/png"
    )
    message_id = upload.json()["message_id"]

    response = await client.get(f"/media/{message_id}", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.content == png_bytes

    other_tenant = await _create_tenant(client, name="Other Co")
    other_headers = _auth(other_tenant["api_key"])
    other_response = await client.get(f"/media/{message_id}", headers=other_headers)
    assert other_response.status_code == 404

    text_response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "user-1", "message": "hi"},
        headers=headers,
    )
    text_message_id = text_response.json()["message_id"]
    text_media_response = await client.get(f"/media/{text_message_id}", headers=headers)
    assert text_media_response.status_code == 404


async def test_conversation_messages_expose_media_url(client, media_root):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_channel(client, headers)
    png_bytes = (FIXTURES / "order_label.png").read_bytes()

    upload = await _upload_media(
        client, headers, channel_id, "user-1", "order_label.png", png_bytes, "image/png"
    )
    conversation_id = upload.json()["conversation_id"]
    media_message_id = upload.json()["message_id"]

    await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "user-1", "message": "oi"},
        headers=headers,
    )

    messages = await client.get(f"/conversations/{conversation_id}/messages", headers=headers)
    by_id = {m["id"]: m for m in messages.json()}

    media_out = by_id[media_message_id]
    assert media_out["media_url"] == f"/media/{media_message_id}"
    assert media_out["media_mime"] == "image/png"

    text_outs = [m for m in messages.json() if m["type"] == "text"]
    assert text_outs
    for text_out in text_outs:
        assert text_out["media_url"] is None
        assert text_out["media_mime"] is None


async def test_duplicate_media_upload_stores_once(client, media_root):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_channel(client, headers)
    png_bytes = (FIXTURES / "order_label.png").read_bytes()

    first = await _upload_media(
        client,
        headers,
        channel_id,
        "user-1",
        "order_label.png",
        png_bytes,
        "image/png",
        message_id="dup-media-1",
    )
    assert first.status_code == 201
    assert first.json()["duplicate"] is False

    second = await _upload_media(
        client,
        headers,
        channel_id,
        "user-1",
        "order_label.png",
        png_bytes,
        "image/png",
        message_id="dup-media-1",
    )
    assert second.status_code == 201
    assert second.json()["duplicate"] is True
    assert second.json()["message_id"] == first.json()["message_id"]

    tenant_dir = media_root / tenant["id"]
    stored_files = list(tenant_dir.glob("*"))
    assert len(stored_files) == 1
