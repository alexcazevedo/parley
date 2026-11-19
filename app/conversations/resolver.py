"""Find-or-create contact + conversation for an inbound message.

A contact is unique per `(tenant_id, channel_id, external_user_id)`. A
conversation is reused while it isn't `CLOSED`; once closed, the next
inbound message starts a fresh `ACTIVE_AI` conversation. The database allows
one open conversation per contact and channel
(`uq_conversations_open_contact_channel`).

Rows are created with `INSERT ... ON CONFLICT DO NOTHING` and selected again,
so two concurrent first messages from one person end up on the same contact
and conversation: the second insert waits for the first transaction, inserts
nothing, and the following select (a new READ COMMITTED snapshot) sees the
committed row. Nothing here raises on a concurrent insert or rolls back the
caller's transaction.
"""

import uuid

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import IncomingMessage
from app.models.channel import Channel
from app.models.contact import Contact
from app.models.conversation import Conversation, ConversationState

# A conflicting open conversation can be closed by the time its transaction
# commits, leaving nothing to select; try again a bounded number of times.
_MAX_TRIES = 3


async def resolve_contact_and_conversation(
    session: AsyncSession, incoming: IncomingMessage
) -> tuple[Contact, Conversation]:
    contact = await _get_or_create_contact(session, incoming)
    conversation = await _get_or_create_conversation(session, incoming, contact)
    return contact, conversation


async def _select_contact(session: AsyncSession, incoming: IncomingMessage) -> Contact | None:
    result = await session.execute(
        select(Contact).where(
            Contact.tenant_id == incoming.tenant_id,
            Contact.channel_id == incoming.channel_id,
            Contact.external_user_id == incoming.external_user_id,
        )
    )
    return result.scalar_one_or_none()


async def _get_or_create_contact(session: AsyncSession, incoming: IncomingMessage) -> Contact:
    contact = await _select_contact(session, incoming)
    if contact is not None:
        return contact

    await session.execute(
        insert(Contact)
        .values(
            id=uuid.uuid4(),
            tenant_id=incoming.tenant_id,
            channel_id=incoming.channel_id,
            external_user_id=incoming.external_user_id,
            attributes={},
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "channel_id", "external_user_id"])
    )
    # Ours, or the one a concurrent request committed while we waited.
    contact = await _select_contact(session, incoming)
    assert contact is not None
    return contact


async def _select_open_conversation(
    session: AsyncSession, incoming: IncomingMessage, contact: Contact
) -> Conversation | None:
    result = await session.execute(
        select(Conversation)
        .where(
            Conversation.tenant_id == incoming.tenant_id,
            Conversation.contact_id == contact.id,
            Conversation.channel_id == incoming.channel_id,
            Conversation.state != ConversationState.CLOSED,
        )
        .order_by(Conversation.created_at.desc())
    )
    return result.scalars().first()


async def _get_or_create_conversation(
    session: AsyncSession, incoming: IncomingMessage, contact: Contact
) -> Conversation:
    conversation = await _select_open_conversation(session, incoming, contact)
    if conversation is not None:
        return conversation

    channel = await session.get(Channel, incoming.channel_id)
    assert channel is not None, "IncomingMessage.channel_id must reference an existing channel"

    for _ in range(_MAX_TRIES):
        await session.execute(
            insert(Conversation)
            .values(
                id=uuid.uuid4(),
                tenant_id=incoming.tenant_id,
                contact_id=contact.id,
                channel_id=incoming.channel_id,
                agent_id=channel.agent_id,
                state=ConversationState.ACTIVE_AI,
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "contact_id", "channel_id"],
                index_where=text("state != 'CLOSED'"),
            )
        )
        conversation = await _select_open_conversation(session, incoming, contact)
        if conversation is not None:
            return conversation
    raise RuntimeError("could not resolve an open conversation")
