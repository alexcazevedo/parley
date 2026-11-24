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

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import end_transaction
from app.media.storage import get_media_storage
from app.media.stt import STTError, transcribe
from app.models.message import Message

logger = structlog.get_logger(__name__)

AUDIO_FAILED_TEXT = "[áudio não compreendido]"
AUDIO_EMPTY_TEXT = "[áudio sem fala detectada]"


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
