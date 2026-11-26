"""First messages cannot collide (PRODUCT.md E4.5, ticket 04).

- Two simultaneous first messages from one new user both succeed and leave
  one contact and one open conversation holding both messages.
- Two simultaneous messages from a known contact whose last conversation is
  closed open exactly one new conversation.
- The database refuses a second open conversation per contact and channel;
  a new one after the first is `CLOSED` is accepted.
"""

import asyncio
import os
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.channels.base import IncomingMessage
from app.messages.ingest import ingest
from app.models.agent import Agent
from app.models.contact import Contact
from app.models.conversation import Conversation, ConversationState
from app.models.message import Message
from app.queue.base import Job, Queue

# Each round is a fresh user; several rounds make an interleaving where both
# requests miss the first select near certain on the current code.
ROUNDS = 5


class _ListQueue(Queue):
    def __init__(self) -> None:
        self.jobs: list[Job] = []

    async def enqueue(self, job: Job) -> None:
        self.jobs.append(job)

    async def dequeue(self, timeout: int = 0) -> Job | None:
        return self.jobs.pop(0) if self.jobs else None

    async def ack(self, job: Job) -> None:
        return None

    async def fail(self, job: Job) -> None:
        return None


async def _channel(client) -> tuple[uuid.UUID, uuid.UUID]:
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    assert tenant.status_code == 201, tenant.text
    headers = {"X-API-Key": tenant.json()["api_key"]}
    agent = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Hi.", "model": "gpt-4o-mini"},
        headers=headers,
    )
    assert agent.status_code == 201, agent.text
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    assert channel.status_code == 201, channel.text
    return uuid.UUID(tenant.json()["id"]), uuid.UUID(channel.json()["id"])


def _incoming(tenant_id, channel_id, user: str, message_id: str) -> IncomingMessage:
    return IncomingMessage(
        tenant_id=tenant_id,
        channel_id=channel_id,
        external_user_id=user,
        external_message_id=message_id,
        type="text",
        content=f"hello {message_id}",
        timestamp=datetime.now(UTC),
    )


async def _ingest_and_commit(engine, incoming: IncomingMessage):
    async with async_sessionmaker(bind=engine, expire_on_commit=False)() as session:
        result = await ingest(session, _ListQueue(), incoming)
        await session.commit()
        return result


async def _race(engine, tenant_id, channel_id, user: str):
    return await asyncio.gather(
        _ingest_and_commit(engine, _incoming(tenant_id, channel_id, user, f"{user}-a")),
        _ingest_and_commit(engine, _incoming(tenant_id, channel_id, user, f"{user}-b")),
    )


async def _state(engine, tenant_id, channel_id, user: str):
    async with async_sessionmaker(bind=engine)() as session:
        contacts = (
            await session.scalars(
                select(Contact).where(
                    Contact.tenant_id == tenant_id,
                    Contact.channel_id == channel_id,
                    Contact.external_user_id == user,
                )
            )
        ).all()
        open_conversations = (
            await session.scalars(
                select(Conversation).where(
                    Conversation.contact_id.in_([c.id for c in contacts]),
                    Conversation.state != ConversationState.CLOSED,
                )
            )
        ).all()
        return contacts, open_conversations


async def _message_count(engine, conversation_id) -> int:
    async with async_sessionmaker(bind=engine)() as session:
        return await session.scalar(
            select(func.count()).select_from(Message).where(
                Message.conversation_id == conversation_id
            )
        )


async def test_two_first_messages_make_one_contact_and_one_conversation(client, test_engine):
    tenant_id, channel_id = await _channel(client)
    for n in range(ROUNDS):
        user = f"race-new-{n}-{uuid.uuid4()}"
        first, second = await _race(test_engine, tenant_id, channel_id, user)

        assert first.conversation_id == second.conversation_id
        assert not first.duplicate and not second.duplicate
        contacts, open_conversations = await _state(test_engine, tenant_id, channel_id, user)
        assert len(contacts) == 1
        assert [c.id for c in open_conversations] == [first.conversation_id]
        assert await _message_count(test_engine, first.conversation_id) == 2


async def test_two_messages_after_close_open_one_new_conversation(client, test_engine):
    tenant_id, channel_id = await _channel(client)
    for n in range(ROUNDS):
        user = f"race-known-{n}-{uuid.uuid4()}"
        opened = await _ingest_and_commit(
            test_engine, _incoming(tenant_id, channel_id, user, f"{user}-0")
        )
        async with async_sessionmaker(bind=test_engine)() as session:
            conversation = await session.get(Conversation, opened.conversation_id)
            conversation.state = ConversationState.CLOSED
            await session.commit()

        first, second = await _race(test_engine, tenant_id, channel_id, user)

        assert first.conversation_id == second.conversation_id != opened.conversation_id
        contacts, open_conversations = await _state(test_engine, tenant_id, channel_id, user)
        assert len(contacts) == 1
        assert [c.id for c in open_conversations] == [first.conversation_id]
        assert await _message_count(test_engine, first.conversation_id) == 2


async def test_database_refuses_second_open_conversation(client, test_engine):
    tenant_id, channel_id = await _channel(client)
    user = f"refuse-{uuid.uuid4()}"
    opened = await _ingest_and_commit(test_engine, _incoming(tenant_id, channel_id, user, "m-1"))

    async with async_sessionmaker(bind=test_engine, expire_on_commit=False)() as session:
        first = await session.get(Conversation, opened.conversation_id)
        agent_id = await session.scalar(select(Agent.id).where(Agent.tenant_id == tenant_id))

        def _another() -> Conversation:
            return Conversation(
                tenant_id=tenant_id,
                contact_id=first.contact_id,
                channel_id=channel_id,
                agent_id=agent_id,
                state=ConversationState.WAITING,
            )

        session.add(_another())
        with pytest.raises(IntegrityError) as excinfo:
            await session.flush()
        assert "uq_conversations_open_contact_channel" in str(excinfo.value)
        await session.rollback()

        first = await session.get(Conversation, opened.conversation_id)
        first.state = ConversationState.CLOSED
        await session.flush()
        session.add(_another())
        await session.flush()
        await session.commit()
