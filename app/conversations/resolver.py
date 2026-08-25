"""Find-or-create contact + conversation for an inbound message.

A contact is unique per `(tenant_id, channel_id, external_user_id)`. A
conversation is reused while it isn't `CLOSED`; once closed, the next
inbound message starts a fresh `ACTIVE_AI` conversation.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import IncomingMessage
from app.models.channel import Channel
from app.models.contact import Contact
from app.models.conversation import Conversation, ConversationState


async def resolve_contact_and_conversation(
    session: AsyncSession, incoming: IncomingMessage
) -> tuple[Contact, Conversation]:
    contact = await _get_or_create_contact(session, incoming)
    conversation = await _get_or_create_conversation(session, incoming, contact)
    return contact, conversation


async def _get_or_create_contact(session: AsyncSession, incoming: IncomingMessage) -> Contact:
    result = await session.execute(
        select(Contact).where(
            Contact.tenant_id == incoming.tenant_id,
            Contact.channel_id == incoming.channel_id,
            Contact.external_user_id == incoming.external_user_id,
        )
    )
    contact = result.scalar_one_or_none()
    if contact is not None:
        return contact

    contact = Contact(
        tenant_id=incoming.tenant_id,
        channel_id=incoming.channel_id,
        external_user_id=incoming.external_user_id,
    )
    session.add(contact)
    await session.flush()
    return contact


async def _get_or_create_conversation(
    session: AsyncSession, incoming: IncomingMessage, contact: Contact
) -> Conversation:
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
    conversation = result.scalars().first()
    if conversation is not None:
        return conversation

    channel = await session.get(Channel, incoming.channel_id)
    assert channel is not None, "IncomingMessage.channel_id must reference an existing channel"

    conversation = Conversation(
        tenant_id=incoming.tenant_id,
        contact_id=contact.id,
        channel_id=incoming.channel_id,
        agent_id=channel.agent_id,
        state=ConversationState.ACTIVE_AI,
    )
    session.add(conversation)
    await session.flush()
    return conversation
