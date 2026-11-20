"""The operator message endpoint does not hold the conversation row lock while
it sends: another session can lock the row during `adapter.send_message`, and a
failed send stores nothing.
"""

import asyncio
import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api import conversations as conversations_api
from app.models.conversation import Conversation
from app.models.message import Message, MessageRole
from app.models.tenant import Tenant
from app.tools.ssrf import SSRFError


class PausedAdapter:
    def __init__(self, error: Exception | None = None) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.error = error

    async def send_message(self, message, contact, channel) -> None:
        self.started.set()
        await self.release.wait()
        if self.error is not None:
            raise self.error


async def _conversation(client) -> uuid.UUID:
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    agent = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Atendente.", "model": "gpt-4o-mini"},
        headers=headers,
    )
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"], "config": {}},
        headers=headers,
    )
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel.json()["id"], "user_id": "user-1", "message": "oi"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["conversation_id"])


async def _post(engine, conversation_id: uuid.UUID):
    async with async_sessionmaker(bind=engine, expire_on_commit=False)() as session:
        conversation = await session.get(Conversation, conversation_id)
        tenant = await session.get(Tenant, conversation.tenant_id)
        return await conversations_api.post_operator_message(
            conversation_id,
            conversations_api.MessageCreate(content="Olá, sou o operador"),
            tenant=tenant,
            db=session,
        )


async def _operator_count(engine, conversation_id: uuid.UUID) -> int:
    async with async_sessionmaker(bind=engine)() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.role == MessageRole.HUMAN_OPERATOR,
                )
            )
        ).scalar_one()


async def test_row_is_lockable_by_another_session_while_the_send_is_paused(
    client, test_engine, monkeypatch
):
    conversation_id = await _conversation(client)
    adapter = PausedAdapter()
    monkeypatch.setattr(conversations_api, "get_adapter", lambda channel_type: adapter)

    task = asyncio.create_task(_post(test_engine, conversation_id))
    try:
        await asyncio.wait_for(adapter.started.wait(), timeout=5)
        async with async_sessionmaker(bind=test_engine)() as other:
            await other.execute(
                select(Conversation.id)
                .where(Conversation.id == conversation_id)
                .with_for_update(nowait=True)
            )
            await other.rollback()
    finally:
        adapter.release.set()
        result = await task

    assert result.content == "Olá, sou o operador"
    assert await _operator_count(test_engine, conversation_id) == 1


@pytest.mark.parametrize("error", [SSRFError("refused"), RuntimeError("boom")])
async def test_failed_send_stores_no_message(client, test_engine, monkeypatch, error):
    conversation_id = await _conversation(client)
    adapter = PausedAdapter(error=error)
    adapter.release.set()
    monkeypatch.setattr(conversations_api, "get_adapter", lambda channel_type: adapter)

    if isinstance(error, SSRFError):
        with pytest.raises(HTTPException) as raised:
            await _post(test_engine, conversation_id)
        assert raised.value.status_code == 502
    else:
        with pytest.raises(RuntimeError):
            await _post(test_engine, conversation_id)

    assert await _operator_count(test_engine, conversation_id) == 0
