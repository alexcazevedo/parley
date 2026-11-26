"""Barge-in: `process_turn`'s two supersede checkpoints (ticket 22).

`process_turn` is driven directly here — the scheduler side (a real Redis
probe, requeueing the outstanding batch) is ticket 23. The conversation is
still built through the API so the rows are real, then the turn is run with a
scripted `supersede_check` whose answers are consumed in order:

    checkpoint 1 (before delivery), then one call per humanizer bubble after
    the first.

Mirrors tests/test_turn_humanize.py: a recording adapter instead of a channel,
`asyncio.sleep` neutralized so the paced delays cost nothing.
"""

import os
import uuid

import structlog
from sqlalchemy import select

import app.handoff.summarizer as summarizer_module
from app.agents.runtime import ERROR_MESSAGE, AgentRuntime
from app.conversations import turn as turn_module
from app.conversations.turn import Superseded, process_turn
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import Agent
from app.models.agent_run import AgentRun, AgentRunStatus
from app.models.message import Message, MessageDirection, MessageRole

MULTI_PART_REPLY = (
    "Oi! Achei seu pedido aqui.\n\n"
    "Ele saiu para entrega hoje de manhã.\n\n"
    "Quer que eu te avise quando o entregador chegar?"
)
PARTS = MULTI_PART_REPLY.split("\n\n")
HANDOFF_MESSAGE = "Já estou te transferindo para uma pessoa do time."
BRIEF_TEXT = "Usuário irritado; bot não resolveu."


class ScriptedCheck:
    """`supersede_check` double: answers from a list, counts every call.

    Past the end of the script it answers `False` (nothing newer arrived).
    """

    def __init__(self, answers: list[bool] | None = None, *, raises: Exception | None = None):
        self.answers = list(answers or [])
        self.raises = raises
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.answers.pop(0) if self.answers else False


class RecordingAdapter:
    """Stands in for the real adapter: records the outbound call sequence."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def parse_incoming(self, payload):  # pragma: no cover - unused here
        raise NotImplementedError

    async def mark_read(self, external_message_id, contact, channel) -> None:
        self.events.append(("mark_read", external_message_id))

    async def send_typing(self, contact, channel, duration_seconds) -> None:
        self.events.append(("typing", duration_seconds))

    async def send_message(self, message, contact, channel) -> None:
        self.events.append(("send", message.content))

    @property
    def sent(self) -> list[str]:
        return [event[1] for event in self.events if event[0] == "send"]


class ExplodingProvider:
    """LLM provider that fails, to drive `AgentRuntime`'s error path."""

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        raise RuntimeError("provider is down")


async def _setup(
    client, db_session, *, humanize: bool, sentiment: bool = False
) -> tuple[dict, str]:
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    agent_response = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Você é o atendente da Acme.", "model": "gpt-4o-mini"},
        headers=headers,
    )
    assert agent_response.status_code == 201, agent_response.text
    if sentiment:
        # Neither field is exposed by the agents API, so they are set on the row.
        agent = await db_session.get(Agent, uuid.UUID(agent_response.json()["id"]))
        agent.sentiment_escalation_enabled = True
        agent.handoff_message = HANDOFF_MESSAGE
        await db_session.commit()

    channel = await client.post(
        "/channels",
        json={
            "type": "webchat",
            "name": "Web",
            "agent_id": agent_response.json()["id"],
            "config": {"humanize": humanize} if humanize else {},
        },
        headers=headers,
    )
    assert channel.status_code == 201, channel.text
    return headers, channel.json()["id"]


async def _send(client, headers, channel_id, text) -> uuid.UUID:
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "user-1", "message": text},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["conversation_id"])


async def _inbound(db_session, conversation_id: uuid.UUID) -> list[Message]:
    result = await db_session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.IN,
        )
        .order_by(Message.seq)
    )
    return list(result.scalars().all())


async def _replies(db_session, conversation_id: uuid.UUID) -> list[str]:
    result = await db_session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.role == MessageRole.ASSISTANT,
            Message.type == "text",
        )
        .order_by(Message.seq)
    )
    return [row.content for row in result.scalars().all()]


async def _agent_run(db_session, conversation_id: uuid.UUID) -> AgentRun | None:
    result = await db_session.execute(
        select(AgentRun).where(AgentRun.conversation_id == conversation_id)
    )
    return result.scalar_one_or_none()


async def _prepare(client, db_session, monkeypatch, *, humanize: bool = True, sentiment=False):
    """Build a real conversation with one inbound message; stub adapter + sleep."""
    headers, channel_id = await _setup(client, db_session, humanize=humanize, sentiment=sentiment)
    conversation_id = await _send(client, headers, channel_id, "cadê meu pedido")
    messages = await _inbound(db_session, conversation_id)
    assert len(messages) == 1

    adapter = RecordingAdapter()
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: adapter)

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(turn_module.asyncio, "sleep", fake_sleep)
    return conversation_id, messages, adapter


def _runtime(reply: str = MULTI_PART_REPLY) -> AgentRuntime:
    return AgentRuntime(provider=FakeLLMProvider([LLMResponse(content=reply)]))


async def test_no_check_delivers_the_whole_reply(client, db_session, monkeypatch):
    """(a) `supersede_check=None` keeps today's behavior byte for byte."""
    conversation_id, messages, adapter = await _prepare(client, db_session, monkeypatch)
    check = ScriptedCheck([True, True, True])

    result = await process_turn(
        db_session, conversation_id, messages, _runtime(), supersede_check=None
    )

    assert isinstance(result, Message)
    assert adapter.sent == PARTS
    assert await _replies(db_session, conversation_id) == PARTS
    assert check.calls == 0


async def test_supersede_before_delivery_sends_nothing(client, db_session, monkeypatch):
    """(b) Checkpoint 1: the reply is dropped whole and the run is marked."""
    conversation_id, messages, adapter = await _prepare(client, db_session, monkeypatch)
    check = ScriptedCheck([True])

    result = await process_turn(
        db_session, conversation_id, messages, _runtime(), supersede_check=check
    )

    assert result == Superseded(0)
    assert check.calls == 1
    assert adapter.sent == []

    # Committed, not merely pending: the rollback below would undo it otherwise.
    await db_session.rollback()
    assert await _replies(db_session, conversation_id) == []
    run = await _agent_run(db_session, conversation_id)
    assert run is not None and run.status == AgentRunStatus.SUPERSEDED


async def test_supersede_between_bubbles_keeps_sent_parts(client, db_session, monkeypatch):
    """(c) Checkpoint 2: part one stands, the rest is dropped."""
    conversation_id, messages, adapter = await _prepare(client, db_session, monkeypatch)
    check = ScriptedCheck([False, True])

    result = await process_turn(
        db_session, conversation_id, messages, _runtime(), supersede_check=check
    )

    assert result == Superseded(1)
    assert check.calls == 2
    assert adapter.sent == [PARTS[0]]

    await db_session.rollback()
    assert await _replies(db_session, conversation_id) == [PARTS[0]]
    run = await _agent_run(db_session, conversation_id)
    assert run is not None and run.status == AgentRunStatus.SUPERSEDED


async def test_quiet_check_delivers_every_part(client, db_session, monkeypatch):
    """(d) One checkpoint before delivery plus one per bubble after the first."""
    conversation_id, messages, adapter = await _prepare(client, db_session, monkeypatch)
    check = ScriptedCheck([False, False, False])

    result = await process_turn(
        db_session, conversation_id, messages, _runtime(), supersede_check=check
    )

    assert isinstance(result, Message)
    assert adapter.sent == PARTS
    assert await _replies(db_session, conversation_id) == PARTS
    assert check.calls == 3


async def test_non_humanized_channel_supersedes_before_its_single_send(
    client, db_session, monkeypatch
):
    """(e) One send, so only checkpoint 1 applies."""
    conversation_id, messages, adapter = await _prepare(
        client, db_session, monkeypatch, humanize=False
    )
    check = ScriptedCheck([True])

    result = await process_turn(
        db_session, conversation_id, messages, _runtime(), supersede_check=check
    )

    assert result == Superseded(0)
    assert check.calls == 1
    assert adapter.sent == []
    assert await _replies(db_session, conversation_id) == []


async def test_failing_check_degrades_to_todays_behavior(client, db_session, monkeypatch):
    """(f) A broken supersede signal never costs the user their reply."""
    conversation_id, messages, adapter = await _prepare(client, db_session, monkeypatch)
    check = ScriptedCheck(raises=RuntimeError("redis is down"))

    with structlog.testing.capture_logs() as logs:
        result = await process_turn(
            db_session, conversation_id, messages, _runtime(), supersede_check=check
        )

    assert isinstance(result, Message)
    assert adapter.sent == PARTS
    assert await _replies(db_session, conversation_id) == PARTS
    assert check.calls == 3
    warnings = [entry for entry in logs if entry["event"] == "turn.supersede_check_failed"]
    assert warnings and all(entry["log_level"] == "warning" for entry in warnings)


async def test_sentiment_escalation_is_never_superseded(client, db_session, monkeypatch):
    """(g) The handoff message is not a stale AI answer — it goes out whole."""
    conversation_id, messages, adapter = await _prepare(
        client, db_session, monkeypatch, sentiment=True
    )

    async def frustrated(session, conversation, agent, input_text, message_ids=()):
        return 0.9

    monkeypatch.setattr(turn_module, "score_frustration", frustrated)
    monkeypatch.setattr(
        summarizer_module,
        "get_provider",
        lambda agent, settings=None: FakeLLMProvider([LLMResponse(content=BRIEF_TEXT)]),
    )
    check = ScriptedCheck([True, True, True])

    result = await process_turn(
        db_session, conversation_id, messages, _runtime(), supersede_check=check
    )

    assert isinstance(result, Message)
    assert check.calls == 0
    assert "".join(adapter.sent) == HANDOFF_MESSAGE
    assert "".join(await _replies(db_session, conversation_id)) == HANDOFF_MESSAGE


async def test_errored_run_keeps_its_error_status(client, db_session, monkeypatch):
    """(h) A superseded errored turn is still an error — don't hide it."""
    conversation_id, messages, adapter = await _prepare(client, db_session, monkeypatch)
    check = ScriptedCheck([True])

    result = await process_turn(
        db_session,
        conversation_id,
        messages,
        AgentRuntime(provider=ExplodingProvider()),
        supersede_check=check,
    )

    assert result == Superseded(0)
    assert adapter.sent == []

    await db_session.rollback()
    run = await _agent_run(db_session, conversation_id)
    assert run is not None
    assert run.status == AgentRunStatus.ERROR
    assert run.output_text == ERROR_MESSAGE
