"""An agent without a `handoff_message` still tells the customer something.

Every handoff path (the tool, the retry of an owed handoff message and the
sentiment handoff) sends `DEFAULT_HANDOFF_MESSAGE` then, never an empty text
and never the "did not understand" reply.

Also: the "message truncated" webhook warning must not carry the sender's id.
"""

# ruff: noqa: F811  (the imported `adapter` fixture is used as a test argument)

import copy
import json
import logging
from pathlib import Path

import respx

from app.conversations import turn as turn_module
from app.conversations.turn import process_turn
from app.models.agent import DEFAULT_HANDOFF_MESSAGE, Agent
from app.models.conversation import Conversation
from app.models.message import Message
from tests.test_evolution import _auth, _create_evolution_channel, _create_tenant
from tests.test_handoff_reply_owed import (  # noqa: F401  (adapter is a fixture)
    _failed_first_attempt,
    _handoff_runtime,
    _inbound,
    _setup,
    _silent_runtime,
    _stored_replies,
    adapter,
)

FIXTURES = Path(__file__).parent / "fixtures"


async def _clear_handoff_message(db_session, conversation_id) -> None:
    conversation = await db_session.get(Conversation, conversation_id)
    agent = await db_session.get(Agent, conversation.agent_id)
    agent.handoff_message = ""
    await db_session.commit()


async def test_retry_of_owed_handoff_message_sends_the_default_text(
    client, fake_redis, db_session, test_engine, adapter
):
    _, _, conversation_id = await _setup(client, fake_redis, db_session)
    await _clear_handoff_message(db_session, conversation_id)
    messages = await _inbound(db_session, conversation_id)
    await _failed_first_attempt(db_session, conversation_id, messages, adapter, _handoff_runtime())

    retried = await process_turn(db_session, conversation_id, messages, _silent_runtime())

    assert isinstance(retried, Message)
    assert adapter.sent == [DEFAULT_HANDOFF_MESSAGE]
    assert await _stored_replies(test_engine, conversation_id) == [DEFAULT_HANDOFF_MESSAGE]


async def test_sentiment_handoff_sends_the_default_text(
    client, fake_redis, db_session, test_engine, adapter, monkeypatch
):
    _, _, conversation_id = await _setup(client, fake_redis, db_session, sentiment=True)
    await _clear_handoff_message(db_session, conversation_id)

    async def frustrated(session, conversation, agent, input_text, message_ids=()) -> float:
        return 0.95

    monkeypatch.setattr(turn_module, "score_frustration", frustrated)
    messages = await _inbound(db_session, conversation_id)

    result = await process_turn(db_session, conversation_id, messages, _silent_runtime())

    assert isinstance(result, Message)
    assert adapter.sent == [DEFAULT_HANDOFF_MESSAGE]
    assert await _stored_replies(test_engine, conversation_id) == [DEFAULT_HANDOFF_MESSAGE]


@respx.mock
async def test_truncated_webhook_warning_has_no_sender_id(client, fake_redis, caplog):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    payload = copy.deepcopy(json.loads((FIXTURES / "evolution_messages_upsert.json").read_text()))
    payload["data"]["message"]["conversation"] = "x" * 50_000

    with caplog.at_level(logging.WARNING, logger="app.api.webhooks"):
        response = await client.post(
            f"/webhooks/whatsapp/{channel_id}",
            json=payload,
            headers={"apikey": "evo-secret-xyz"},
        )

    assert response.status_code == 200, response.text
    [record] = [r for r in caplog.records if "truncated" in r.getMessage()]
    text = record.getMessage()
    assert "5511999998888" not in text
    assert str(channel_id) in text
    assert "3EB0C767D26A1D5F9E1A" in text
