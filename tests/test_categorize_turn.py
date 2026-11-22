"""Categorization wired into `process_turn`, through the real pipeline.

Same pipeline as tests/test_sentiment_escalation.py (ingest -> debounce ->
scheduler tick). The classifier's provider is a `FakeLLMProvider` patched in
as `app.agents.categorize.get_provider`; where only the call site matters,
`turn.categorize_conversation` itself is replaced by a recorder.
"""

import os
import time
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import select

import app.agents.categorize as categorize_module
import app.handoff.summarizer as summarizer_module
from app.conversations import debounce, retry
from app.conversations import turn as turn_module
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import Agent
from app.models.conversation import Conversation, ConversationState
from app.models.message import Message, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.workers.main import run_once
from app.workers.scheduler import tick

DEBOUNCE_SECONDS = 0.2
NORMAL_REPLY = "Claro! Deixa eu verificar isso pra você."
HANDOFF_MESSAGE = "Já estou te transferindo para uma pessoa do time."


class RecordingRuntime:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def run(self, conversation, input_text: str) -> str:
        self.calls.append(input_text)
        return NORMAL_REPLY


class RecordingAdapter:
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


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


class Pipeline:
    """One tenant, agent and webchat channel; sends batches and runs ticks."""

    def __init__(self, client, fake_redis, db_session, monkeypatch) -> None:
        self.client = client
        self.fake_redis = fake_redis
        self.db_session = db_session
        self.monkeypatch = monkeypatch
        self.runtime = RecordingRuntime()
        self.adapter = RecordingAdapter()
        self.categories: dict[str, uuid.UUID] = {}

    async def setup(self, *, categories=(), sentiment_enabled=False) -> None:
        tenant = await self.client.post(
            "/tenants",
            json={"name": "Acme"},
            headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]},
        )
        self.headers = {"X-API-Key": tenant.json()["api_key"]}
        agent = await self.client.post(
            "/agents",
            json={"name": "Bot", "instructions": "x", "model": "gpt-4o-mini"},
            headers=self.headers,
        )
        assert agent.status_code == 201, agent.text
        row = await self.db_session.get(Agent, uuid.UUID(agent.json()["id"]))
        row.sentiment_escalation_enabled = sentiment_enabled
        row.handoff_message = HANDOFF_MESSAGE
        await self.db_session.commit()
        channel = await self.client.post(
            "/channels",
            json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
            headers=self.headers,
        )
        assert channel.status_code == 201, channel.text
        self.channel_id = channel.json()["id"]
        for name, description in categories:
            response = await self.client.post(
                "/categories", json={"name": name, "description": description}, headers=self.headers
            )
            assert response.status_code == 201, response.text
            self.categories[name] = uuid.UUID(response.json()["id"])

        self.monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: self.adapter)
        self.monkeypatch.setattr(
            summarizer_module,
            "get_provider",
            lambda agent, settings=None: FakeLLMProvider([LLMResponse(content="brief")]),
        )

        async def fake_sleep(seconds: float) -> None:
            return None

        self.monkeypatch.setattr(turn_module.asyncio, "sleep", fake_sleep)

    def classifier_answers(self, *answers: str) -> FakeLLMProvider:
        provider = FakeLLMProvider([LLMResponse(content=answer) for answer in answers])
        self.monkeypatch.setattr(
            categorize_module, "get_provider", lambda agent, settings=None: provider
        )
        return provider

    async def batch(self, text: str) -> uuid.UUID:
        response = await self.client.post(
            "/channels/webchat/messages",
            json={"channel_id": self.channel_id, "user_id": "user-1", "message": text},
            headers=self.headers,
        )
        assert response.status_code == 201, response.text
        queue = RedisQueue(self.fake_redis)
        while await self.fake_redis.llen(JOBS_KEY) > 0:
            assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None
        await tick(
            self.fake_redis,
            _session_factory(self.db_session),
            self.runtime,
            now=time.time() + 5,
        )
        return uuid.UUID(response.json()["conversation_id"])

    async def stored(self, conversation_id: uuid.UUID):
        return (
            await self.db_session.execute(
                select(Conversation.category_id, Conversation.state).where(
                    Conversation.id == conversation_id
                )
            )
        ).one()

    async def set_state(self, conversation_id: uuid.UUID, state: ConversationState) -> None:
        conversation = await self.db_session.get(Conversation, conversation_id)
        conversation.state = state
        await self.db_session.commit()

    async def replies(self, conversation_id: uuid.UUID) -> list[str]:
        result = await self.db_session.execute(
            select(Message.content)
            .where(
                Message.conversation_id == conversation_id,
                Message.role == MessageRole.ASSISTANT,
                Message.type == "text",
            )
            .order_by(Message.seq)
        )
        return list(result.scalars().all())


CATEGORIES = (("billing", "invoices and payments"), ("support", "help with the product"))


async def test_batches_file_and_move_the_conversation(client, fake_redis, db_session, monkeypatch):
    pipeline = Pipeline(client, fake_redis, db_session, monkeypatch)
    await pipeline.setup(categories=CATEGORIES)
    provider = pipeline.classifier_answers("1", "2", "0")

    conversation_id = await pipeline.batch("quero a segunda via do boleto")
    assert (await pipeline.stored(conversation_id)).category_id == pipeline.categories["billing"]

    await pipeline.batch("o app não abre")
    assert (await pipeline.stored(conversation_id)).category_id == pipeline.categories["support"]

    # Nothing fits: the model never clears the category.
    await pipeline.batch("obrigado")
    assert (await pipeline.stored(conversation_id)).category_id == pipeline.categories["support"]

    assert provider.call_count == 3
    assert pipeline.runtime.calls == ["quero a segunda via do boleto", "o app não abre", "obrigado"]
    assert await pipeline.replies(conversation_id) == [NORMAL_REPLY] * 3


async def test_tenant_without_categories_makes_no_call(client, fake_redis, db_session, monkeypatch):
    pipeline = Pipeline(client, fake_redis, db_session, monkeypatch)
    await pipeline.setup()
    provider = pipeline.classifier_answers()

    conversation_id = await pipeline.batch("oi")

    assert provider.call_count == 0
    assert (await pipeline.stored(conversation_id)).category_id is None
    assert pipeline.runtime.calls == ["oi"]
    assert await pipeline.replies(conversation_id) == [NORMAL_REPLY]


async def test_classifier_failure_still_gets_the_reply(client, fake_redis, db_session, monkeypatch):
    pipeline = Pipeline(client, fake_redis, db_session, monkeypatch)
    await pipeline.setup(categories=CATEGORIES)
    provider = pipeline.classifier_answers()  # out of scripted answers: the call raises

    conversation_id = await pipeline.batch("oi")

    assert provider.call_count == 1
    assert (await pipeline.stored(conversation_id)).category_id is None
    assert await pipeline.replies(conversation_id) == [NORMAL_REPLY]


async def test_human_handoff_batch_is_categorized_without_a_reply(
    client, fake_redis, db_session, monkeypatch
):
    pipeline = Pipeline(client, fake_redis, db_session, monkeypatch)
    await pipeline.setup(categories=CATEGORIES)
    provider = pipeline.classifier_answers("2", "1")

    conversation_id = await pipeline.batch("o app não abre")
    assert (await pipeline.stored(conversation_id)).category_id == pipeline.categories["support"]
    await pipeline.set_state(conversation_id, ConversationState.HUMAN_HANDOFF)
    sends_before = len(pipeline.adapter.events)

    await pipeline.batch("na verdade é sobre o boleto")

    stored = await pipeline.stored(conversation_id)
    assert stored.category_id == pipeline.categories["billing"]
    assert stored.state == ConversationState.HUMAN_HANDOFF
    assert provider.call_count == 2
    assert "na verdade é sobre o boleto" in provider.calls[1]["messages"][1].content
    assert pipeline.runtime.calls == ["o app não abre"]
    assert pipeline.adapter.events[sends_before:] == []
    assert await pipeline.replies(conversation_id) == [NORMAL_REPLY]


class CallRecorder:
    def __init__(self, order: list[str], name: str, result=None) -> None:
        self.order = order
        self.name = name
        self.result = result
        self.calls: list[tuple[str, list]] = []
        self.conversation_ids: list[uuid.UUID] = []

    async def __call__(self, session, conversation, agent, input_text, message_ids=()):
        self.order.append(self.name)
        self.conversation_ids.append(conversation.id)
        self.calls.append((input_text, list(message_ids)))
        return self.result


@pytest.mark.parametrize("state", [ConversationState.WAITING, ConversationState.CLOSED])
async def test_waiting_and_closed_are_not_categorized(
    state, client, fake_redis, db_session, monkeypatch
):
    pipeline = Pipeline(client, fake_redis, db_session, monkeypatch)
    await pipeline.setup(categories=CATEGORIES)
    pipeline.classifier_answers("1")
    conversation_id = await pipeline.batch("oi")
    await pipeline.set_state(conversation_id, state)
    recorder = CallRecorder([], "categorize")
    monkeypatch.setattr(turn_module, "categorize_conversation", recorder)

    await pipeline.batch("outra coisa")

    # A closed conversation's next message opens a new, AI-driven one, which
    # is categorized as usual; the WAITING or CLOSED one never is.
    assert conversation_id not in recorder.conversation_ids
    stored = await pipeline.stored(conversation_id)
    assert stored.category_id == pipeline.categories["billing"]
    assert stored.state == state


async def test_categorized_before_sentiment_with_the_batch_ids(
    client, fake_redis, db_session, monkeypatch
):
    pipeline = Pipeline(client, fake_redis, db_session, monkeypatch)
    await pipeline.setup(categories=CATEGORIES, sentiment_enabled=True)
    order: list[str] = []
    categorize = CallRecorder(order, "categorize")
    score = CallRecorder(order, "score", result=0.1)
    monkeypatch.setattr(turn_module, "categorize_conversation", categorize)
    monkeypatch.setattr(turn_module, "score_frustration", score)

    conversation_id = await pipeline.batch("oi")

    assert order == ["categorize", "score"]
    batch_ids = list(
        (
            await db_session.execute(
                select(Message.id).where(
                    Message.conversation_id == conversation_id, Message.role == MessageRole.USER
                )
            )
        ).scalars()
    )
    assert categorize.calls == [("oi", batch_ids)]
    assert score.calls == [("oi", batch_ids)]


async def test_escalated_conversation_is_handed_off_categorized(
    client, fake_redis, db_session, monkeypatch
):
    pipeline = Pipeline(client, fake_redis, db_session, monkeypatch)
    await pipeline.setup(categories=CATEGORIES, sentiment_enabled=True)
    pipeline.classifier_answers("1")
    monkeypatch.setattr(turn_module, "score_frustration", CallRecorder([], "score", result=0.95))

    conversation_id = await pipeline.batch("cobraram duas vezes, que absurdo")

    stored = await pipeline.stored(conversation_id)
    assert stored.state == ConversationState.HUMAN_HANDOFF
    assert stored.category_id == pipeline.categories["billing"]
    assert pipeline.runtime.calls == []
    assert await pipeline.replies(conversation_id) == [HANDOFF_MESSAGE]
    conversation = await db_session.get(Conversation, conversation_id)
    await db_session.refresh(conversation)
    assert conversation.waiting_since is not None
    lines = (await client.get("/lines", headers=pipeline.headers)).json()
    billing_line = next(
        line for line in lines if line["category_id"] == str(pipeline.categories["billing"])
    )
    assert [c["id"] for c in billing_line["conversations"]] == [str(conversation_id)]


async def test_agent_load_failure_in_a_handoff_batch_does_not_fail_the_turn(
    client, fake_redis, db_session, monkeypatch
):
    pipeline = Pipeline(client, fake_redis, db_session, monkeypatch)
    await pipeline.setup(categories=CATEGORIES)
    provider = pipeline.classifier_answers("2", "1")
    conversation_id = await pipeline.batch("o app não abre")
    await pipeline.set_state(conversation_id, ConversationState.HUMAN_HANDOFF)

    async def boom(session, conversation):
        raise RuntimeError("database went away")

    monkeypatch.setattr(turn_module, "load_conversation_agent", boom)

    await pipeline.batch("na verdade é sobre o boleto")  # must not raise

    stored = await pipeline.stored(conversation_id)
    assert stored.state == ConversationState.HUMAN_HANDOFF
    assert stored.category_id == pipeline.categories["support"]
    assert provider.call_count == 1
    # The turn finished: no retry was recorded and nothing is due again.
    assert await fake_redis.exists(retry.attempts_key(conversation_id)) == 0
    assert await fake_redis.zcard(debounce.DUE_KEY) == 0
