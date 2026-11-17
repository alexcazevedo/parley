"""Tenant-scoped agent loading (PRODUCT.md F3).

A database written by an older version can hold a channel or conversation
whose `agent_id` points at another tenant's agent. Such a conversation must
never run that agent: the turn is refused (nothing sent, no `AgentRun`), the
runtime raises, and neither the rolling summary nor the handoff brief is
produced. Same-tenant conversations keep working.

Tenant A owns an agent; tenant B owns a channel, contact and conversation.
The cross-tenant link is written straight onto B's rows, the way a legacy
database would hold it — the API refuses to create it.
"""

import os
import uuid
from pathlib import Path

import httpx
import pytest
import respx
import structlog
from sqlalchemy import select

import app.handoff.summarizer as summarizer_module
from app.agents.runtime import AgentRuntime
from app.config import get_settings
from app.conversations import turn as turn_module
from app.conversations.turn import EchoRuntime, process_turn
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import Agent
from app.models.agent_run import AgentRun
from app.models.channel import Channel
from app.models.conversation import Conversation
from app.models.message import Message, MessageDirection

VOICE_NOTE = Path(__file__).parent / "fixtures" / "voice_note.mp3"
REPLY = "Olá! Como posso ajudar?"
BRIEF_TEXT = "Brief that must never be written for a cross-tenant agent."


class RecordingAdapter:
    """Stands in for the channel adapter: records every outbound call."""

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


class RecordingRuntime:
    """Text-in/text-out runtime that records calls and exposes `llm_provider`."""

    def __init__(self, provider: FakeLLMProvider) -> None:
        self.provider = provider
        self.calls: list[str] = []
        self.provider_requests: list[Agent] = []

    async def run(self, conversation, input_text: str) -> str:
        self.calls.append(input_text)
        return REPLY

    def llm_provider(self, agent: Agent) -> FakeLLMProvider:
        self.provider_requests.append(agent)
        return self.provider


async def _tenant(client, name: str) -> dict:
    response = await client.post(
        "/tenants", json={"name": name}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    assert response.status_code == 201, response.text
    return {"X-API-Key": response.json()["api_key"]}


async def _agent(client, headers: dict, instructions: str) -> uuid.UUID:
    response = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": instructions, "model": "gpt-4o-mini"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _channel(client, headers: dict, agent_id: uuid.UUID) -> uuid.UUID:
    response = await client.post(
        "/channels",
        json={
            "type": "webchat",
            "name": "Web",
            "agent_id": str(agent_id),
            # Humanized so a non-refused turn would send a read receipt first.
            "config": {"humanize": True},
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _send(client, headers: dict, channel_id: uuid.UUID, text: str) -> uuid.UUID:
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": str(channel_id), "user_id": "user-1", "message": text},
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
        .order_by(Message.created_at)
    )
    return list(result.scalars().all())


async def _outbound(db_session, conversation_id: uuid.UUID) -> list[Message]:
    result = await db_session.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.OUT,
        )
    )
    return list(result.scalars().all())


async def _runs(db_session, conversation_id: uuid.UUID) -> list[AgentRun]:
    result = await db_session.execute(
        select(AgentRun).where(AgentRun.conversation_id == conversation_id)
    )
    return list(result.scalars().all())


@pytest.fixture
def adapter(monkeypatch) -> RecordingAdapter:
    recording = RecordingAdapter()
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: recording)

    async def fake_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(turn_module.asyncio, "sleep", fake_sleep)
    return recording


async def _upload_audio(client, headers: dict, channel_id: uuid.UUID) -> uuid.UUID:
    response = await client.post(
        "/channels/webchat/media",
        data={"channel_id": str(channel_id), "user_id": "user-1"},
        files={"file": ("voice_note.mp3", VOICE_NOTE.read_bytes(), "audio/mpeg")},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["conversation_id"])


async def _cross_linked(
    client, db_session, *, audio: bool = False
) -> tuple[uuid.UUID, uuid.UUID, list[Message]]:
    """B's conversation (and channel) pointing at A's agent.

    With `audio`, the inbound message is a voice note instead of text.
    Returns `(conversation_id, foreign_agent_id, inbound_messages)`.
    """
    headers_a = await _tenant(client, "Tenant A")
    foreign_agent_id = await _agent(client, headers_a, "SECRET instructions of tenant A.")

    headers_b = await _tenant(client, "Tenant B")
    own_agent_id = await _agent(client, headers_b, "Você é o atendente do tenant B.")
    channel_id = await _channel(client, headers_b, own_agent_id)
    if audio:
        conversation_id = await _upload_audio(client, headers_b, channel_id)
    else:
        conversation_id = await _send(client, headers_b, channel_id, "oi, preciso de ajuda")

    # The legacy link, written directly: the API would refuse it.
    channel = await db_session.get(Channel, channel_id)
    conversation = await db_session.get(Conversation, conversation_id)
    channel.agent_id = foreign_agent_id
    conversation.agent_id = foreign_agent_id
    await db_session.commit()

    foreign = await db_session.get(Agent, foreign_agent_id)
    assert foreign.tenant_id != conversation.tenant_id

    messages = await _inbound(db_session, conversation_id)
    assert len(messages) == 1
    return conversation_id, foreign_agent_id, messages


async def test_loader_refuses_foreign_agent_and_logs(client, db_session):
    from app.agents.loading import load_conversation_agent

    conversation_id, foreign_agent_id, _ = await _cross_linked(client, db_session)
    conversation = await db_session.get(Conversation, conversation_id)
    foreign = await db_session.get(Agent, foreign_agent_id)

    with structlog.testing.capture_logs() as logs:
        agent = await load_conversation_agent(db_session, conversation)

    assert agent is None
    mismatches = [entry for entry in logs if entry["event"] == "agent.tenant_mismatch"]
    assert len(mismatches) == 1
    entry = mismatches[0]
    assert entry["log_level"] == "warning"
    assert entry["conversation_id"] == str(conversation.id)
    assert entry["agent_id"] == str(foreign_agent_id)
    assert entry["conversation_tenant_id"] == str(conversation.tenant_id)
    assert entry["agent_tenant_id"] == str(foreign.tenant_id)


async def test_loader_returns_same_tenant_agent_and_none_for_missing(client, db_session):
    from app.agents.loading import load_conversation_agent

    headers = await _tenant(client, "Solo")
    agent_id = await _agent(client, headers, "Você é o atendente.")
    channel_id = await _channel(client, headers, agent_id)
    conversation_id = await _send(client, headers, channel_id, "oi")
    conversation = await db_session.get(Conversation, conversation_id)

    agent = await load_conversation_agent(db_session, conversation)
    assert agent is not None and agent.id == agent_id

    # A dangling id (no such row) is "unavailable" too. Transient object only:
    # the FK forbids writing it to the table.
    ghost = Conversation(
        id=uuid.uuid4(),
        tenant_id=conversation.tenant_id,
        contact_id=conversation.contact_id,
        channel_id=conversation.channel_id,
        agent_id=uuid.uuid4(),
    )
    assert await load_conversation_agent(db_session, ghost) is None


async def test_agent_runtime_turn_is_refused(client, db_session, adapter):
    conversation_id, _, messages = await _cross_linked(client, db_session)
    provider = FakeLLMProvider([LLMResponse(content=REPLY)])

    with structlog.testing.capture_logs() as logs:
        result = await process_turn(
            db_session, conversation_id, messages, AgentRuntime(provider=provider)
        )

    assert result is None
    assert adapter.events == []
    assert provider.call_count == 0
    await db_session.rollback()
    assert await _outbound(db_session, conversation_id) == []
    assert await _runs(db_session, conversation_id) == []

    refused = [entry for entry in logs if entry["event"] == "turn.refused_agent_unavailable"]
    assert len(refused) == 1
    assert refused[0]["log_level"] == "warning"
    assert refused[0]["conversation_id"] == str(conversation_id)


async def test_echo_runtime_turn_is_refused(client, db_session, adapter):
    conversation_id, _, messages = await _cross_linked(client, db_session)

    result = await process_turn(db_session, conversation_id, messages, EchoRuntime())

    assert result is None
    assert adapter.events == []
    await db_session.rollback()
    assert await _outbound(db_session, conversation_id) == []
    assert await _runs(db_session, conversation_id) == []


async def test_refused_turn_skips_sentiment_check(client, db_session, adapter, monkeypatch):
    conversation_id, foreign_agent_id, messages = await _cross_linked(client, db_session)
    foreign = await db_session.get(Agent, foreign_agent_id)
    foreign.sentiment_escalation_enabled = True
    await db_session.commit()

    scored: list[str] = []

    async def score(session, conversation, agent, input_text):
        scored.append(input_text)
        return 1.0

    monkeypatch.setattr(turn_module, "score_frustration", score)
    runtime = RecordingRuntime(FakeLLMProvider([LLMResponse(content="resumo")]))

    result = await process_turn(db_session, conversation_id, messages, runtime)

    assert result is None
    assert scored == []
    assert runtime.calls == []
    assert adapter.events == []


async def test_refused_turn_makes_no_speech_to_text_call(
    client, db_session, adapter, tmp_path, monkeypatch
):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    try:
        conversation_id, _, messages = await _cross_linked(client, db_session, audio=True)
        assert messages[0].type == "audio" and not (messages[0].content or "").strip()
        provider = FakeLLMProvider([LLMResponse(content=REPLY)])

        with respx.mock(assert_all_called=False) as router:
            stt = router.post(url__regex=r".*/audio/transcriptions$").mock(
                return_value=httpx.Response(200, json={"text": "transcrição"})
            )
            result = await process_turn(
                db_session, conversation_id, messages, AgentRuntime(provider=provider)
            )
            assert stt.call_count == 0

        assert result is None
        assert provider.call_count == 0
        assert adapter.events == []
    finally:
        get_settings.cache_clear()


async def test_run_turn_raises_for_foreign_agent(client, db_session):
    conversation_id, _, _ = await _cross_linked(client, db_session)
    conversation = await db_session.get(Conversation, conversation_id)
    provider = FakeLLMProvider([LLMResponse(content=REPLY)])

    with pytest.raises(RuntimeError):
        await AgentRuntime(provider=provider).run_turn(db_session, conversation, "oi")

    assert provider.call_count == 0
    await db_session.flush()
    assert await _runs(db_session, conversation_id) == []


async def test_rolling_summary_skipped_for_foreign_agent(client, db_session):
    conversation_id, _, _ = await _cross_linked(client, db_session)
    conversation = await db_session.get(Conversation, conversation_id)
    provider = FakeLLMProvider([LLMResponse(content="resumo")])
    runtime = RecordingRuntime(provider)

    await turn_module._maybe_summarize(db_session, conversation, runtime)

    assert runtime.provider_requests == []
    assert provider.call_count == 0


async def test_handoff_summary_skipped_for_foreign_agent(client, db_session, monkeypatch):
    conversation_id, _, _ = await _cross_linked(client, db_session)
    conversation = await db_session.get(Conversation, conversation_id)
    provider = FakeLLMProvider([LLMResponse(content=BRIEF_TEXT)])
    monkeypatch.setattr(summarizer_module, "get_provider", lambda agent, settings=None: provider)

    summary = await summarizer_module.generate_handoff_summary(
        db_session, conversation, "cliente pediu atendente"
    )

    assert summary is None
    assert provider.call_count == 0


async def test_same_tenant_conversation_still_gets_reply(client, db_session, adapter):
    headers = await _tenant(client, "Solo")
    agent_id = await _agent(client, headers, "Você é o atendente.")
    channel_id = await _channel(client, headers, agent_id)
    conversation_id = await _send(client, headers, channel_id, "oi")
    messages = await _inbound(db_session, conversation_id)
    provider = FakeLLMProvider([LLMResponse(content=REPLY)])

    result = await process_turn(
        db_session, conversation_id, messages, AgentRuntime(provider=provider)
    )

    assert isinstance(result, Message)
    assert result.content == REPLY
    assert ("send", REPLY) in adapter.events
    assert provider.call_count == 1
    assert len(await _runs(db_session, conversation_id)) == 1
