"""External channel media: image, voice note and PDF through
`POST /channels/external/media`, then the turn (run in-process, scripted model).
PDFs are built here with `pypdf`; speech-to-text is mocked with respx.
"""

import io
import uuid

import httpx
import respx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from sqlalchemy import select

from app.agents.runtime import AgentRuntime
from app.config import get_settings
from app.conversations.turn import process_turn
from app.llm.base import ROLE_USER, ImagePart, LLMResponse, TextPart
from app.llm.providers.fake import FakeLLMProvider
from app.media.pdf import extract_pdf_text
from app.models.message import Message, MessageDirection, MessageRole
from tests.test_audio_turn import TRANSCRIPT, _mp3_bytes
from tests.test_external_channel import REPLY, _inbound, _setup
from tests.test_vision_turn import _png_bytes

UNSUPPORTED_TEXT = "Não consigo ler esse tipo de arquivo."


def _pdf(text: str | None = None, pages: int = 1) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        page = writer.add_blank_page(width=200, height=200)
        if text is not None:
            font = writer._add_object(
                DictionaryObject(
                    {
                        NameObject("/Type"): NameObject("/Font"),
                        NameObject("/Subtype"): NameObject("/Type1"),
                        NameObject("/BaseFont"): NameObject("/Helvetica"),
                    }
                )
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
            )
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode())
            page[NameObject("/Contents")] = writer._add_object(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


async def _upload(client, headers, channel_id, data, name, ctype, path="external", **form):
    return await client.post(
        f"/channels/{path}/media",
        data={"channel_id": channel_id, "user_id": "cust-1", **form},
        files={"file": (name, data, ctype)},
        headers=headers,
    )


async def _turn(db_session, conversation_id, provider):
    messages = await _inbound(db_session, conversation_id)
    return await process_turn(
        db_session, conversation_id, messages, AgentRuntime(provider=provider)
    )


def _user_contents(provider):
    return [m.content for m in provider.calls[0]["messages"] if m.role == ROLE_USER]


async def _setup_media(client, tmp_path, monkeypatch, unsupported=None):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    headers, channel_id, _ = await _setup(client)
    if unsupported:
        agents = (await client.get("/agents", headers=headers)).json()
        items = agents if isinstance(agents, list) else agents["items"]
        resp = await client.patch(
            f"/agents/{items[0]['id']}",
            json={"unsupported_media_message": unsupported},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
    return headers, channel_id


async def test_pdf_text_reaches_the_model(client, db_session, tmp_path, monkeypatch):
    headers, channel_id = await _setup_media(client, tmp_path, monkeypatch)
    pdf = _pdf("Pedido 12345 atrasado")
    response = await _upload(
        client, headers, channel_id, pdf, "nota.pdf", "application/pdf", caption="veja"
    )
    assert response.status_code == 201, response.text
    body = response.json()

    provider = FakeLLMProvider([LLMResponse(content=REPLY)])
    await _turn(db_session, uuid.UUID(body["conversation_id"]), provider)

    assert provider.call_count == 1
    assert _user_contents(provider) == ["veja\n\n[documento: nota.pdf]\nPedido 12345 atrasado"]
    message = (
        await db_session.execute(select(Message).where(Message.id == uuid.UUID(body["message_id"])))
    ).scalar_one()
    assert message.type == "document"
    assert message.media_mime == "application/pdf"
    assert message.raw_payload["document_status"] == "ok"

    served = await client.get(f"/media/{body['message_id']}", headers=headers)
    assert served.status_code == 200
    assert served.content == pdf
    assert served.headers["content-type"] == "application/pdf"
    assert served.headers["content-disposition"].startswith("attachment")
    assert served.headers["x-content-type-options"] == "nosniff"


async def test_blank_pdf_gets_the_unsupported_text_and_no_model_call(
    client, db_session, tmp_path, monkeypatch
):
    headers, channel_id = await _setup_media(client, tmp_path, monkeypatch, UNSUPPORTED_TEXT)
    response = await _upload(client, headers, channel_id, _pdf(), "vazio.pdf", "application/pdf")
    assert response.status_code == 201

    provider = FakeLLMProvider([])
    await _turn(db_session, uuid.UUID(response.json()["conversation_id"]), provider)

    assert provider.call_count == 0
    reply = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == uuid.UUID(response.json()["conversation_id"]),
                Message.direction == MessageDirection.OUT,
            )
        )
    ).scalar_one()
    assert reply.role == MessageRole.ASSISTANT
    assert reply.content == UNSUPPORTED_TEXT


async def test_corrupt_pdf_gets_the_unsupported_text_and_no_model_call(
    client, db_session, tmp_path, monkeypatch
):
    headers, channel_id = await _setup_media(client, tmp_path, monkeypatch, UNSUPPORTED_TEXT)
    response = await _upload(
        client, headers, channel_id, b"%PDF-1.4 not really", "x.pdf", "application/pdf"
    )
    assert response.status_code == 201

    provider = FakeLLMProvider([])
    await _turn(db_session, uuid.UUID(response.json()["conversation_id"]), provider)

    assert provider.call_count == 0
    reply = (
        await db_session.execute(
            select(Message).where(
                Message.conversation_id == uuid.UUID(response.json()["conversation_id"]),
                Message.direction == MessageDirection.OUT,
            )
        )
    ).scalar_one()
    assert reply.content == UNSUPPORTED_TEXT


async def test_pdf_text_is_cut_at_the_limits(monkeypatch):
    data = _pdf("abcdefghij", pages=3)
    assert extract_pdf_text(data, 3, 100) == "abcdefghij\n\nabcdefghij\n\nabcdefghij"
    assert extract_pdf_text(data, 2, 100) == "abcdefghij\n\nabcdefghij"
    assert extract_pdf_text(data, 3, 5) == "abcde"
    assert extract_pdf_text(b"garbage", 3, 100) == ""


async def test_oversize_file_is_413(client, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_MAX_BYTES", "2000")
    headers, channel_id = await _setup_media(client, tmp_path, monkeypatch)
    big = _pdf("x") + b"0" * 5000
    response = await _upload(client, headers, channel_id, big, "big.pdf", "application/pdf")
    assert response.status_code == 413
    get_settings.cache_clear()


async def test_image_reaches_the_model_as_an_image(client, db_session, tmp_path, monkeypatch):
    headers, channel_id = await _setup_media(client, tmp_path, monkeypatch)
    response = await _upload(
        client, headers, channel_id, _png_bytes(), "a.png", "image/png", caption="olha"
    )
    assert response.status_code == 201

    provider = FakeLLMProvider([LLMResponse(content=REPLY)])
    await _turn(db_session, uuid.UUID(response.json()["conversation_id"]), provider)

    parts = _user_contents(provider)[0]
    assert [p.data for p in parts if isinstance(p, ImagePart)] == [_png_bytes()]
    assert [p.text for p in parts if isinstance(p, TextPart)] == ["olha"]


async def test_voice_note_is_transcribed(client, db_session, tmp_path, monkeypatch):
    headers, channel_id = await _setup_media(client, tmp_path, monkeypatch)
    response = await _upload(client, headers, channel_id, _mp3_bytes(), "v.mp3", "audio/mpeg")
    assert response.status_code == 201

    provider = FakeLLMProvider([LLMResponse(content=REPLY)])
    async with respx.mock(assert_all_called=False) as router:
        route = router.post("https://api.openai.com/v1/audio/transcriptions").mock(
            return_value=httpx.Response(200, json={"text": TRANSCRIPT})
        )
        await _turn(db_session, uuid.UUID(response.json()["conversation_id"]), provider)

    assert route.called
    assert _user_contents(provider) == [TRANSCRIPT]


async def test_webchat_still_refuses_a_pdf(client, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    headers, channel_id, _ = await _setup(client, channel_type="webchat")
    response = await _upload(
        client, headers, channel_id, _pdf("x"), "n.pdf", "application/pdf", path="webchat"
    )
    assert response.status_code == 415


async def test_external_media_to_a_webchat_channel_is_refused(client, tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    headers, channel_id, _ = await _setup(client, channel_type="webchat")
    response = await _upload(client, headers, channel_id, _png_bytes(), "a.png", "image/png")
    assert response.status_code == 422
