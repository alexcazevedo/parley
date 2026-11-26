"""The sentiment prompt holds the current batch exactly once (ticket 03)."""

import os
import uuid

import app.agents.sentiment as sentiment_module
from app.agents.sentiment import SENTIMENT_MESSAGE_LIMIT, score_frustration
from app.llm.base import LLMResponse
from app.models.agent import Agent
from app.models.conversation import Conversation
from app.models.message import Message, MessageDirection, MessageRole


class CapturingProvider:
    def __init__(self) -> None:
        self.prompts: list = []

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        self.prompts.append(messages)
        return LLMResponse(content="0.5")

    @property
    def transcript_lines(self) -> list[str]:
        body = self.prompts[-1][1].content
        return body.removeprefix("Conversation transcript:\n").split("\n")


async def _conversation(client, db_session) -> tuple[Conversation, Agent]:
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    agent = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "x", "model": "gpt-4o-mini"},
        headers=headers,
    )
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    sent = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel.json()["id"], "user_id": "u", "message": "h0"},
        headers=headers,
    )
    assert sent.status_code == 201, sent.text
    conversation = await db_session.get(Conversation, uuid.UUID(sent.json()["conversation_id"]))
    return conversation, await db_session.get(Agent, uuid.UUID(agent.json()["id"]))


async def _add(db_session, conversation, role, content, type_="text") -> Message:
    message = Message(
        id=uuid.uuid4(),
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        channel_id=conversation.channel_id,
        direction=MessageDirection.IN if role == MessageRole.USER else MessageDirection.OUT,
        role=role,
        type=type_,
        content=content,
    )
    db_session.add(message)
    await db_session.flush()
    return message


async def _score(db_session, monkeypatch, conversation, agent, input_text, ids):
    provider = CapturingProvider()
    monkeypatch.setattr(sentiment_module, "get_provider", lambda agent, settings=None: provider)
    kwargs = {"message_ids": ids} if ids is not None else {}
    await score_frustration(db_session, conversation, agent, input_text, **kwargs)
    return provider


async def test_batch_appears_once_after_history(client, db_session, monkeypatch):
    conversation, agent = await _conversation(client, db_session)  # h0 is history
    await _add(db_session, conversation, MessageRole.ASSISTANT, "h1")
    await _add(db_session, conversation, MessageRole.USER, "h2")
    b1 = await _add(db_session, conversation, MessageRole.USER, "batch-one")
    b2 = await _add(db_session, conversation, MessageRole.USER, "batch-two")

    provider = await _score(
        db_session, monkeypatch, conversation, agent, "batch-one\nbatch-two", [b1.id, b2.id]
    )

    assert provider.transcript_lines == [
        "user: h0",
        "assistant: h1",
        "user: h2",
        "user: batch-one",
        "user: batch-two",
    ]


async def test_audio_transcript_appears_once(client, db_session, monkeypatch):
    conversation, agent = await _conversation(client, db_session)
    audio = await _add(db_session, conversation, MessageRole.USER, "meu pedido atrasou", "audio")

    text = "meu pedido atrasou"
    provider = await _score(db_session, monkeypatch, conversation, agent, text, [audio.id])

    assert "\n".join(provider.transcript_lines).count("meu pedido atrasou") == 1


async def test_history_limit_applies_to_earlier_messages(client, db_session, monkeypatch):
    conversation, agent = await _conversation(client, db_session)
    for i in range(1, SENTIMENT_MESSAGE_LIMIT + 2):
        await _add(db_session, conversation, MessageRole.USER, f"old{i}")
    batch = await _add(db_session, conversation, MessageRole.USER, "the-batch")

    provider = await _score(db_session, monkeypatch, conversation, agent, "the-batch", [batch.id])

    lines = provider.transcript_lines
    assert len(lines) == SENTIMENT_MESSAGE_LIMIT + 1
    assert lines[-1] == "user: the-batch"
    assert lines[0] == f"user: old{2}"


async def test_whitespace_history_skipped(client, db_session, monkeypatch):
    conversation, agent = await _conversation(client, db_session)
    await _add(db_session, conversation, MessageRole.ASSISTANT, "   ")
    batch = await _add(db_session, conversation, MessageRole.USER, "now")

    provider = await _score(db_session, monkeypatch, conversation, agent, "now", [batch.id])

    assert provider.transcript_lines == ["user: h0", "user: now"]


async def test_without_message_ids_nothing_is_dropped(client, db_session, monkeypatch):
    conversation, agent = await _conversation(client, db_session)
    await _add(db_session, conversation, MessageRole.USER, "now")

    provider = await _score(db_session, monkeypatch, conversation, agent, "now", None)

    assert provider.transcript_lines == ["user: h0", "user: now", "user: now"]
