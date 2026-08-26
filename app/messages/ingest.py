"""Inbound ingestion: resolve contact/conversation, idempotently persist the
message, and enqueue a job for the worker.

Callers (`app/api/webchat.py`, `app/api/dev.py`, and the WhatsApp webhook in
a later ticket) all funnel through `ingest()` — it's the one place duplicate
deliveries are collapsed and jobs are produced.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import AUDIO_MIMES, IMAGE_MIMES, IncomingMessage
from app.config import get_settings
from app.conversations.resolver import resolve_contact_and_conversation
from app.media.storage import get_media_storage
from app.models.message import Message, MessageDirection, MessageRole
from app.queue.base import Job, Queue

_MIMES_BY_TYPE = {"image": IMAGE_MIMES, "audio": AUDIO_MIMES}


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


async def _find_existing(
    session: AsyncSession, incoming: IncomingMessage
) -> uuid.UUID | None:
    result = await session.execute(
        select(Message.id).where(
            Message.tenant_id == incoming.tenant_id,
            Message.channel_id == incoming.channel_id,
            Message.external_message_id == incoming.external_message_id,
        )
    )
    return result.scalar_one_or_none()


async def ingest(session: AsyncSession, queue: Queue, incoming: IncomingMessage) -> IngestResult:
    _contact, conversation = await resolve_contact_and_conversation(session, incoming)

    media_key: str | None = None
    media_mime: str | None = None

    if incoming.media is not None:
        mime = incoming.media.mime.split(";", 1)[0].strip()
        allowed = _MIMES_BY_TYPE.get(incoming.type, set())
        if mime not in allowed:
            raise MediaRejected(f"unsupported mime {mime!r} for type {incoming.type!r}")

        settings = get_settings()
        if len(incoming.media.data) > settings.media_max_bytes:
            raise MediaRejected("media exceeds max size")

        # Look up a duplicate before storing anything — no orphan file gets
        # written if this exact delivery was already ingested.
        if incoming.external_message_id:
            existing_id = await _find_existing(session, incoming)
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
        existing_id = await _find_existing(session, incoming)
        assert existing_id is not None
        return IngestResult(
            conversation_id=conversation.id, message_id=existing_id, duplicate=True
        )

    await queue.enqueue(Job(conversation_id=conversation.id, message_id=inserted_id, attempt=0))
    return IngestResult(conversation_id=conversation.id, message_id=inserted_id, duplicate=False)
