"""Inbound ingestion: resolve contact/conversation, idempotently persist the
message, and enqueue a job for the worker.

Callers (`app/api/webchat.py`, `app/api/dev.py`, and the WhatsApp webhook)
all funnel through `ingest()` — it's the one place duplicate deliveries are
collapsed and jobs are produced. The WhatsApp webhook also asks
`find_existing` first, so a repeated delivery costs nothing else, and passes
no media: its image/audio rows are stored with `media_key` null and the
worker downloads them (`app/media/fetch.py`).
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import (
    AUDIO_MIMES,
    DOCUMENT_MIMES,
    IMAGE_MIMES,
    IncomingMedia,
    IncomingMessage,
)
from app.config import get_settings
from app.conversations.resolver import resolve_contact_and_conversation
from app.media.sniff import media_type_for, sniff_mime
from app.media.storage import get_media_storage
from app.models.conversation import Conversation
from app.models.message import Message, MessageDirection, MessageRole
from app.queue.base import Job, Queue

_MIMES_BY_TYPE = {"image": IMAGE_MIMES, "audio": AUDIO_MIMES, "document": DOCUMENT_MIMES}


class MediaRejected(Exception):
    """Raised when an inbound media message fails validation — the API maps
    this to 415 (unsupported mime) or 413 (oversize).
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass
class IngestResult:
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    duplicate: bool


def validate_media(message_type: str, media: IncomingMedia) -> str:
    """Check inbound media against the rules for `message_type`; return its mime.

    The declared mime (before any `;codecs=` suffix) must be allowed for the
    type, the data must fit `settings.media_max_bytes`, and the sniffed
    content type must match the type. The sniffed mime is returned: it, not
    the declared one, is what gets stored. Raises `MediaRejected` otherwise.
    Shared by `ingest` (webchat uploads) and the worker's Evolution download
    (`app/media/fetch.py`).
    """
    mime = media.mime.split(";", 1)[0].strip()
    allowed = _MIMES_BY_TYPE.get(message_type, set())
    if mime not in allowed:
        raise MediaRejected(f"unsupported mime {mime!r} for type {message_type!r}")

    if len(media.data) > get_settings().media_max_bytes:
        raise MediaRejected("media exceeds max size")

    sniffed = sniff_mime(media.data)
    if sniffed is None or media_type_for(sniffed) != message_type:
        raise MediaRejected("unsupported media content")
    return sniffed


async def find_existing(
    session: AsyncSession, incoming: IncomingMessage
) -> uuid.UUID | None:
    """Id of the message already stored for this delivery (same tenant,
    channel and `external_message_id`), or `None`."""
    result = await session.execute(
        select(Message.id).where(
            Message.tenant_id == incoming.tenant_id,
            Message.channel_id == incoming.channel_id,
            Message.external_message_id == incoming.external_message_id,
        )
    )
    return result.scalar_one_or_none()


async def ingest(
    session: AsyncSession, queue: Queue, incoming: IncomingMessage, *, enqueue: bool = True
) -> IngestResult:
    """Persist `incoming` idempotently. With `enqueue=False` the message row
    is written (visible to operators) but no job is produced, so no agent
    turn runs — used for rate-limited webhooks.
    """
    _contact, conversation = await resolve_contact_and_conversation(session, incoming)
    # Held until the caller commits, so this message (and its `seq`) is
    # visible before the rolling summary can move past it
    # (app/conversations/summary.py).
    await session.execute(
        select(Conversation.id).where(Conversation.id == conversation.id).with_for_update()
    )

    media_key: str | None = None
    media_mime: str | None = None

    if incoming.media is not None:
        mime = validate_media(incoming.type, incoming.media)

        # Look up a duplicate before storing anything — no orphan file gets
        # written if this exact delivery was already ingested.
        if incoming.external_message_id:
            existing_id = await find_existing(session, incoming)
            if existing_id is not None:
                return IngestResult(
                    conversation_id=conversation.id, message_id=existing_id, duplicate=True
                )

        storage = get_media_storage()
        media_key = await storage.put(incoming.tenant_id, incoming.media.data, mime)
        media_mime = mime

    message_id = uuid.uuid4()
    stmt = (
        insert(Message)
        .values(
            id=message_id,
            tenant_id=incoming.tenant_id,
            conversation_id=conversation.id,
            channel_id=incoming.channel_id,
            direction=MessageDirection.IN,
            role=MessageRole.USER,
            type=incoming.type,
            content=incoming.content,
            external_message_id=incoming.external_message_id,
            raw_payload=incoming.raw,
            media_key=media_key,
            media_mime=media_mime,
        )
        .on_conflict_do_nothing(
            index_elements=["tenant_id", "channel_id", "external_message_id"],
            index_where=text("external_message_id IS NOT NULL"),
        )
        .returning(Message.id)
    )
    result = await session.execute(stmt)
    inserted_id = result.scalar_one_or_none()

    if inserted_id is None:
        existing_id = await find_existing(session, incoming)
        assert existing_id is not None
        return IngestResult(
            conversation_id=conversation.id, message_id=existing_id, duplicate=True
        )

    if enqueue:
        await queue.enqueue(
            Job(conversation_id=conversation.id, message_id=inserted_id, attempt=0)
        )
    return IngestResult(conversation_id=conversation.id, message_id=inserted_id, duplicate=False)
