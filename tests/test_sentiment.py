"""Unit tests for the frustration classifier (app/agents/sentiment.py).

Scripted-provider pattern mirrors tests/test_handoff_summary.py: the
classifier is exercised directly against in-memory (unpersisted) Agent /
Conversation objects, monkeypatching `get_provider` so no real LLM call or
even a persisted row is needed to prove the parsing/timeout/failure
contract. No turn wiring here (ticket 21) — just the classifier module.
"""

import asyncio
import uuid

import app.agents.sentiment as sentiment_module
from app.agents.sentiment import score_frustration
from app.config import Settings
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import Agent
from app.models.conversation import Conversation


class RaisingProvider:
    """Always fails — proves the classifier's LLM failure path is harmless."""

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        raise RuntimeError("llm is down")


class SlowProvider:
    """Sleeps past any sane timeout — proves the wait_for guard fires."""

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        await asyncio.sleep(0.5)
        return LLMResponse(content="0.9")


def _agent(**overrides) -> Agent:
    return Agent(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="Bot",
        instructions="be nice",
        model="gpt-4o-mini",
        **overrides,
    )


def _conversation(agent: Agent) -> Conversation:
    return Conversation(
        id=uuid.uuid4(),
        tenant_id=agent.tenant_id,
        contact_id=uuid.uuid4(),
        channel_id=uuid.uuid4(),
        agent_id=agent.id,
    )


async def _score(db_session, monkeypatch, content: str | None = None, provider=None):
    agent = _agent()
    conversation = _conversation(agent)
    scripted = provider or FakeLLMProvider([LLMResponse(content=content)])
    monkeypatch.setattr(sentiment_module, "get_provider", lambda agent, settings=None: scripted)
    return await score_frustration(db_session, conversation, agent, "isso não resolve nada")


async def test_bare_number_parses(db_session, monkeypatch):
    assert await _score(db_session, monkeypatch, content="0.85") == 0.85


async def test_embedded_number_parses(db_session, monkeypatch):
    assert await _score(db_session, monkeypatch, content="Score: 0.9/1") == 0.9


async def test_garbage_returns_none(db_session, monkeypatch):
    assert await _score(db_session, monkeypatch, content="não sei dizer") is None


async def test_provider_raising_returns_none(db_session, monkeypatch):
    assert await _score(db_session, monkeypatch, provider=RaisingProvider()) is None


async def test_timeout_returns_none(db_session, monkeypatch):
    monkeypatch.setattr(
        sentiment_module, "get_settings", lambda: Settings(sentiment_timeout_seconds=0.01)
    )
    assert await _score(db_session, monkeypatch, provider=SlowProvider()) is None


async def test_high_value_clamped_to_one(db_session, monkeypatch):
    assert await _score(db_session, monkeypatch, content="1.7") == 1.0


async def test_bootstrap_new_tenant_creates_agent_with_sentiment_enabled(client, db_session):
    response = await client.post("/chat/bootstrap")
    assert response.status_code == 200
    agent_id = uuid.UUID(response.json()["agent_id"])

    agent = await db_session.get(Agent, agent_id)
    assert agent.sentiment_escalation_enabled is True


async def test_bootstrap_upgrades_existing_agent_flag_to_true(client, db_session):
    response = await client.post("/chat/bootstrap")
    assert response.status_code == 200
    agent_id = uuid.UUID(response.json()["agent_id"])

    agent = await db_session.get(Agent, agent_id)
    agent.sentiment_escalation_enabled = False
    await db_session.commit()

    response2 = await client.post("/chat/bootstrap")
    assert response2.status_code == 200
    assert response2.json()["agent_id"] == str(agent_id)

    await db_session.refresh(agent)
    assert agent.sentiment_escalation_enabled is True
