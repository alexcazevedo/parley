"""Audio transcription: runs before the turn joins the batch into text.

For each audio message freshly ingested this turn, load its bytes from
storage and transcribe them, writing the transcript into `Message.content` so
`render_message_text` — and everything that reads it downstream:
sentiment, the rolling summary, the operator page — treats it exactly like a
text message. STT failure degrades to a fixed placeholder rather than costing
the customer a reply (PRODUCT.md's graceful-degradation rule).

Idempotent by construction: only messages whose `content` is still empty are
touched, so a re-run (barge-in requeue, a retried job) never transcribes the
same voice note twice — and never overwrites an already-failed transcript
with a second attempt either, since a failed transcription also sets
`content` to a non-empty placeholder.
"""

import asyncio
import re

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import end_transaction
from app.media.pdf import extract_pdf_text
from app.media.storage import get_media_storage
from app.media.stt import STTError, transcribe
from app.models.message import Message

logger = structlog.get_logger(__name__)

AUDIO_FAILED_TEXT = "[áudio não compreendido]"
AUDIO_EMPTY_TEXT = "[áudio sem fala detectada]"


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _document_name(message: Message) -> str:
    """The uploaded file name for the model: one line, no control characters."""
    media = (message.raw_payload or {}).get("media") or {}
    name = _CONTROL_CHARS.sub(" ", str(media.get("filename") or "")).strip()
    return name[:120] or "documento.pdf"


async def _prepare_document(session: AsyncSession, message: Message) -> None:
    """Put a PDF's text into `content`: `<caption>\\n\\n[documento: <name>]\\n<text>`.

    A PDF with no text (scanned, blank, broken, encrypted) becomes an
    `unsupported` message, which the turn answers without a model call.
    Idempotent through `raw_payload["document_status"]`.
    """
    if (message.raw_payload or {}).get("document_status"):
        return
    settings = get_settings()
    try:
        data = await get_media_storage().get(message.media_key or "")
    except OSError as exc:
        logger.warning("media.document_unreadable", message_id=str(message.id), error=str(exc))
        data = b""
    # No transaction while the file is parsed.
    await end_transaction(session)
    text = await asyncio.to_thread(
        extract_pdf_text, data, settings.pdf_max_pages, settings.pdf_max_chars
    )
    if not text:
        message.type = "unsupported"
        message.raw_payload = {**(message.raw_payload or {}), "document_status": "empty"}
        return
    caption = (message.content or "").strip()
    body = f"[documento: {_document_name(message)}]\n{text}"
    message.content = f"{caption}\n\n{body}" if caption else body
    message.raw_payload = {**(message.raw_payload or {}), "document_status": "ok"}


async def prepare_media_messages(session: AsyncSession, messages: list[Message]) -> None:
    """Transcribe every untranscribed audio message in `messages`, in place.

    The session's transaction is ended (`app.db.end_transaction`) right
    before each transcription call, and never when nothing needs
    transcribing. Rows written before it are therefore committed and no
    longer roll back if the turn fails later.
    """
    settings = get_settings()
    storage = get_media_storage()

    for message in messages:
        if message.type == "document" and message.media_key:
            await _prepare_document(session, message)
            continue
        if message.type != "audio" or not message.media_key:
            continue
        if (message.content or "").strip():
            continue  # already transcribed (ok or failed) — never redo the work

        try:
            data = await storage.get(message.media_key)
            # No transaction while speech-to-text runs: what the turn wrote
            # so far (earlier transcripts included) is committed here.
            await end_transaction(session)
            transcript = await transcribe(
                data, message.media_mime or "audio/mpeg", settings=settings
            )
            message.content = transcript.strip() or AUDIO_EMPTY_TEXT
            message.raw_payload = {**(message.raw_payload or {}), "transcript_status": "ok"}
        except (STTError, OSError) as exc:
            message.content = AUDIO_FAILED_TEXT
            message.raw_payload = {**(message.raw_payload or {}), "transcript_status": "failed"}
            logger.warning(
                "media.transcription_failed",
                message_id=str(message.id),
                media_key=message.media_key,
                error=str(exc),
            )

    await session.flush()
