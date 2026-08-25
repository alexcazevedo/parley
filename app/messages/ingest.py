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

from app.channels.base import IncomingMessage
from app.conversations.resolver import resolve_contact_and_conversation
from app.models.message import Message, MessageDirection, MessageRole
from app.queue.base import Job, Queue


@dataclass
class IngestResult:
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    duplicate: bool


async def ingest(session: AsyncSession, queue: Queue, incoming: IncomingMessage) -> IngestResult:
    _contact, conversation = await resolve_contact_and_conversation(session, incoming)

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
        existing = await session.execute(
            select(Message.id).where(
                Message.tenant_id == incoming.tenant_id,
                Message.channel_id == incoming.channel_id,
                Message.external_message_id == incoming.external_message_id,
            )
        )
        existing_id = existing.scalar_one()
        return IngestResult(
            conversation_id=conversation.id, message_id=existing_id, duplicate=True
        )

    await queue.enqueue(Job(conversation_id=conversation.id, message_id=inserted_id, attempt=0))
    return IngestResult(conversation_id=conversation.id, message_id=inserted_id, duplicate=False)
