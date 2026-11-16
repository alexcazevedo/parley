"""Upload hardening: size cap while receiving, type decided from content."""

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.media.sniff import sniff_mime
from app.models.message import Message
from tests.test_ingest import _auth, _create_channel, _create_tenant
from tests.test_media_ingest import FIXTURES, _upload_media, media_root  # noqa: F401

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "image/jpeg"),
        (PNG, "image/png"),
        (b"RIFF\x00\x00\x00\x00WEBPVP8 ", "image/webp"),
        (b"RIFF\x00\x00\x00\x00WAVEfmt ", "audio/wav"),
        (b"OggS\x00\x02", "audio/ogg"),
        (b"ID3\x04\x00", "audio/mpeg"),
        (b"\xff\xf3\xc4\xc4\x00", "audio/mpeg"),
        (b"\x00\x00\x00\x20ftypM4A ", "audio/mp4"),
        (b"\x1a\x45\xdf\xa3\x01", "audio/webm"),
        (b"\x00\x00\x00\x18ftypheic\x00\x00", None),
        (b"\x00\x00\x00\x14ftypqt  \x00\x00", None),
        (b"\x00\x00\x00\x20ftypavif\x00\x00", None),
        (b"\x00\x00\x00\x20ftypisom\x00\x00", "audio/mp4"),
        (b"\xff\xff\xff\xff" + b"junk", None),
        (b"\xff\xfb\x90\x00", "audio/mpeg"),
        (b"<html><script>alert(1)</script></html>", None),
        (b"", None),
        (b"RIFF\x00\x00\x00\x00AVI ", None),
    ],
)
def test_sniff_mime_table(data, expected):
    assert sniff_mime(data) == expected


async def _setup(client):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_channel(client, headers)
    return tenant, headers, channel_id


async def _stored(db_session, message_id):
    import uuid

    result = await db_session.execute(select(Message).where(Message.id == uuid.UUID(message_id)))
    return result.scalar_one()


async def test_html_labeled_png_is_rejected(client, media_root):  # noqa: F811
    _, headers, channel_id = await _setup(client)
    response = await _upload_media(
        client, headers, channel_id, "u", "x.png", b"<html>hi</html>", "image/png"
    )
    assert response.status_code == 415


async def test_png_labeled_text_plain_is_stored_as_png(client, db_session, media_root):  # noqa: F811
    _, headers, channel_id = await _setup(client)
    png = (FIXTURES / "order_label.png").read_bytes()
    response = await _upload_media(client, headers, channel_id, "u", "x.txt", png, "text/plain")
    assert response.status_code == 201
    message = await _stored(db_session, response.json()["message_id"])
    assert message.type == "image"
    assert message.media_mime == "image/png"


async def test_png_labeled_audio_is_stored_as_image(client, db_session, media_root):  # noqa: F811
    _, headers, channel_id = await _setup(client)
    png = (FIXTURES / "order_label.png").read_bytes()
    response = await _upload_media(client, headers, channel_id, "u", "x.mp3", png, "audio/mpeg")
    assert response.status_code == 201
    message = await _stored(db_session, response.json()["message_id"])
    assert message.type == "image"
    assert message.media_mime == "image/png"


async def test_body_over_cap_is_413(client, media_root, monkeypatch):  # noqa: F811
    _, headers, channel_id = await _setup(client)
    monkeypatch.setenv("MEDIA_MAX_BYTES", "10")
    get_settings.cache_clear()
    import app.api.webchat as webchat

    reached = []

    async def spy(*args, **kwargs):
        reached.append(1)

    monkeypatch.setattr(webchat, "ingest_message", spy)
    png = (FIXTURES / "order_label.png").read_bytes()
    response = await _upload_media(client, headers, channel_id, "u", "x.png", png, "image/png")
    assert response.status_code == 413
    assert reached == []  # cut off while reading, before ingest


async def test_oversized_content_length_is_413_before_handler(client, media_root, monkeypatch):  # noqa: F811
    _, headers, channel_id = await _setup(client)
    called = []
    import app.api.webchat as webchat

    async def boom(*args, **kwargs):
        called.append(1)

    monkeypatch.setattr(webchat, "resolve_channel", boom)
    big = {**headers, "Content-Length": str(get_settings().media_max_bytes + 1_048_577)}
    response = await client.post(
        "/channels/webchat/media",
        data={"channel_id": channel_id, "user_id": "u"},
        files={"file": ("x.png", PNG, "image/png")},
        headers=big,
    )
    assert response.status_code == 413
    assert called == []


async def test_receive_wrapper_counts_bytes_not_header(client, media_root, monkeypatch):  # noqa: F811
    from app.main import app

    monkeypatch.setenv("MEDIA_MAX_BYTES", "10")
    get_settings.cache_clear()
    try:
        limit = get_settings().media_max_bytes + 1_048_576
        chunk = b"x" * 65536
        sent = []

        head = b'--abc\r\nContent-Disposition: form-data; name="file"; filename="a.png"\r\n\r\n'

        async def receive():
            sent.append(1)
            body = head if len(sent) == 1 else chunk
            return {"type": "http.request", "body": body, "more_body": True}

        responses = []

        async def send(message):
            responses.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/channels/webchat/media",
            "raw_path": b"/channels/webchat/media",
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"test"),
                (b"content-length", b"10"),
                (b"transfer-encoding", b"chunked"),
                (b"content-type", b"multipart/form-data; boundary=abc"),
                (b"authorization", b"Bearer x"),
            ],
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
        }
        await app(scope, receive, send)
        start = next(m for m in responses if m["type"] == "http.response.start")
        assert start["status"] == 413
        assert len(sent) * len(chunk) <= limit + 2 * len(chunk)
        assert len(sent) < 1000
    finally:
        get_settings.cache_clear()


async def test_json_message_without_content_length_is_not_411(client):
    _, headers, channel_id = await _setup(client)

    async def body():
        yield b'{"channel_id": "' + channel_id.encode() + b'", "user_id": "u", "message": "hi"}'

    response = await client.post(
        "/channels/webchat/messages",
        content=body(),
        headers={**headers, "Content-Type": "application/json"},
    )
    assert "content-length" not in response.request.headers
    assert response.status_code == 201
