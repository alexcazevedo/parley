"""Success demo of PRODUCT.md (E4 part 1): one test per clause, through the
public entry points. Helpers come from the per-feature test modules."""

import uuid

import pytest
import respx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agents.runtime import AgentRuntime
from app.conversations import turn as turn_module
from app.conversations.turn import Superseded, process_turn
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import Agent
from app.models.agent_run import AgentRunStatus
from app.models.contact import Contact
from app.models.conversation import Conversation, ConversationState
from app.models.message import Message
from app.queue.redis_queue import JOBS_KEY
from tests import test_handoff_barge_in as barge
from tests import test_operator_takeover as takeover
from tests.test_evolution import _auth, _create_evolution_channel, _create_tenant
from tests.test_evolution_jids import _counts, _payload
from tests.test_first_message_race import (
    _channel,
    _incoming,
    _ingest_and_commit,
    _race,
    _state,
)


async def test_handoff_made_during_a_turn_is_announced_despite_a_newer_message(
    client, db_session, test_engine, monkeypatch
):
    conversation_id, messages, adapter = await barge._prepare(
        client, db_session, monkeypatch, humanize=False, handoff_message=barge.HANDOFF_MESSAGE
    )
    result = await process_turn(
        db_session,
        conversation_id,
        messages,
        barge._handoff_runtime(),
        supersede_check=barge.ScriptedCheck(default=True),
    )
    assert isinstance(result, Message)
    assert adapter.sent == [barge.HANDOFF_MESSAGE]
    assert await barge._committed_state(test_engine, conversation_id) == (
        ConversationState.HUMAN_HANDOFF
    )


async def test_operator_handoff_during_paced_reply_stops_bubbles_and_ai(
    client, db_session, test_engine, monkeypatch
):
    conversation_id, messages, adapter, _ = await takeover._prepare(
        client, db_session, monkeypatch, humanize=True
    )
    sleeps = 0

    async def pausing_sleep(seconds: float) -> None:
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            await takeover._operator(test_engine, conversation_id, "handoff")

    monkeypatch.setattr(turn_module.asyncio, "sleep", pausing_sleep)

    await process_turn(
        db_session, conversation_id, messages, takeover.TextRuntime(takeover.MULTI_PART_REPLY)
    )

    assert adapter.sent == [takeover.PARTS[0]]
    assert await takeover._stored_replies(test_engine, conversation_id) == [takeover.PARTS[0]]
    assert await takeover._committed_state(test_engine, conversation_id) == (
        ConversationState.HUMAN_HANDOFF
    )


@respx.mock
async def test_groups_and_status_ignored_and_lid_answered(client, fake_redis, db_session):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    url = f"/webhooks/whatsapp/{channel_id}"
    headers = {"apikey": "evo-secret-xyz"}

    for jid in ("120363025@g.us", "status@broadcast"):
        response = await client.post(url, json=_payload(jid), headers=headers)
        assert response.json() == {"status": "ignored"}
    assert await _counts(db_session, tenant["id"]) == (0, 0, 0)
    assert await fake_redis.llen(JOBS_KEY) == 0

    response = await client.post(url, json=_payload("1234567890@lid"), headers=headers)
    assert response.status_code == 200
    assert response.json() != {"status": "ignored"}
    contact = (
        await db_session.execute(
            select(Contact).where(Contact.tenant_id == uuid.UUID(tenant["id"]))
        )
    ).scalar_one()
    assert contact.external_user_id == "1234567890@lid"
    assert await fake_redis.llen(JOBS_KEY) == 1


async def test_two_simultaneous_first_messages_leave_one_contact_one_conversation(
    client, test_engine
):
    tenant_id, channel_id = await _channel(client)
    user = f"demo-{uuid.uuid4()}"
    first, second = await _race(test_engine, tenant_id, channel_id, user)
    assert first.conversation_id == second.conversation_id
    contacts, open_conversations = await _state(test_engine, tenant_id, channel_id, user)
    assert len(contacts) == 1
    assert [c.id for c in open_conversations] == [first.conversation_id]


async def test_database_refuses_a_second_open_conversation(client, test_engine):
    tenant_id, channel_id = await _channel(client)
    opened = await _ingest_and_commit(
        test_engine, _incoming(tenant_id, channel_id, f"u-{uuid.uuid4()}", "m-1")
    )
    async with async_sessionmaker(bind=test_engine, expire_on_commit=False)() as session:
        first = await session.get(Conversation, opened.conversation_id)
        agent_id = await session.scalar(select(Agent.id).where(Agent.tenant_id == tenant_id))
        session.add(
            Conversation(
                tenant_id=tenant_id,
                contact_id=first.contact_id,
                channel_id=channel_id,
                agent_id=agent_id,
                state=ConversationState.WAITING,
            )
        )
        with pytest.raises(IntegrityError) as excinfo:
            await session.flush()
        assert "uq_conversations_open_contact_channel" in str(excinfo.value)


async def test_superseded_turn_marks_its_own_run(client, db_session, test_engine, monkeypatch):
    conversation_id, messages, adapter = await barge._prepare(
        client, db_session, monkeypatch, humanize=False, handoff_message=barge.HANDOFF_MESSAGE
    )
    other_run_id = await barge._other_turns_run(db_session, conversation_id)
    runtime = AgentRuntime(provider=FakeLLMProvider([LLMResponse(content="Claro.")]))

    result = await process_turn(
        db_session, conversation_id, messages, runtime, supersede_check=barge.ScriptedCheck()
    )

    assert result == Superseded(0)
    statuses = await barge._run_statuses(test_engine, conversation_id)
    assert statuses.pop(other_run_id) == AgentRunStatus.SUCCESS
    assert list(statuses.values()) == [AgentRunStatus.SUPERSEDED]
