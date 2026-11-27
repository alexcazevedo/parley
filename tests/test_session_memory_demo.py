"""PRODUCT.md's success demo for session memory, end to end.

One customer on an external channel, through the HTTP API and the worker passes
(`end_silent_sessions`, `write_recaps`, the turn). Only the model is scripted
(`FakeLLMProvider`; its recorded calls are the prompts). Time passes by moving
message times in the database, never by waiting.
"""

import os
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, timedelta

import pytest
from sqlalchemy import func, select, update

import app.conversations.session_end as session_end
from app.agents.context import RECAPS_HEADER
from app.agents.runtime import AgentRuntime
from app.conversations.session_end import end_silent_sessions, write_recaps
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.memory.service import MemoryService
from app.models.conversation import Conversation
from app.models.conversation_session import ConversationSession
from app.models.memory import Memory, MemoryType
from app.models.message import Message
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from app.workers.scheduler import tick

DEBOUNCE_SECONDS = 0.2
THREE_DAYS = 72


@pytest.fixture(autouse=True)
async def _only_this_test(db_session):
    """Rows other tests left open or pending are not this test's to recap."""
    await db_session.execute(
        update(ConversationSession)
        .where(ConversationSession.status.in_(["open", "pending"]))
        .values(status="done")
    )
    await db_session.commit()


class Demo:
    """One tenant with an external channel; customers write through the API."""

    def __init__(self, client, fake_redis, db_session, monkeypatch) -> None:
        self.client = client
        self.fake_redis = fake_redis
        self.db = db_session
        self.monkeypatch = monkeypatch
        self.headers: dict[str, str] = {}
        self.channel_id = ""
        self.provider: FakeLLMProvider | None = None

    @asynccontextmanager
    async def factory(self):
        yield self.db

    async def setup(self, **agent_overrides) -> "Demo":
        tenant = await self.client.post(
            "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
        )
        self.headers = {"X-API-Key": tenant.json()["api_key"]}
        agent = await self.client.post(
            "/agents",
            json={
                "name": "Bot",
                "instructions": "Você é o atendente da Acme.",
                "model": "gpt-4o-mini",
                "boundary_check_enabled": False,
                "summary_threshold": 0,
                **agent_overrides,
            },
            headers=self.headers,
        )
        assert agent.status_code == 201, agent.text
        channel = await self.client.post(
            "/channels",
            json={
                "type": "external",
                "name": "project inbox",
                "agent_id": agent.json()["id"],
                "config": {"events_url": "https://inbox.example.com/parley/events"},
            },
            headers=self.headers,
        )
        assert channel.status_code == 201, channel.text
        self.channel_id = channel.json()["id"]
        return self

    async def send(self, text: str, user: str = "cust-1") -> uuid.UUID:
        response = await self.client.post(
            "/channels/external/messages",
            json={"channel_id": self.channel_id, "user_id": user, "message": text},
            headers=self.headers,
        )
        assert response.status_code == 201, response.text
        return uuid.UUID(response.json()["conversation_id"])

    async def turn(self, reply: str) -> str:
        """Run the worker until the agent answered; return the prompt of that call."""
        provider = FakeLLMProvider([LLMResponse(content=reply)], repeat_last=True)
        queue = RedisQueue(self.fake_redis)
        while await self.fake_redis.llen(JOBS_KEY) > 0:
            assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None
        runtime = AgentRuntime(provider=provider, registry=ToolRegistry([]))
        await tick(self.fake_redis, self.factory, runtime, now=time.time() + 5)
        assert provider.calls, "the agent was not called"
        self.provider = provider
        return self.prompt()

    def messages(self) -> list:
        assert self.provider is not None
        return list(self.provider.calls[-1]["messages"])

    def prompt(self) -> str:
        return "\n".join(str(m.content) for m in self.messages())

    async def chat(self, text: str, reply: str, user: str = "cust-1") -> tuple[uuid.UUID, str]:
        conversation_id = await self.send(text, user)
        return conversation_id, await self.turn(reply)

    async def age(self, conversation_id, hours: float) -> None:
        await self.db.execute(
            update(Message)
            .where(Message.conversation_id == conversation_id)
            .values(created_at=Message.created_at - timedelta(hours=hours))
        )
        await self.db.commit()

    def script_recaps(self, *answers: str) -> FakeLLMProvider:
        provider = FakeLLMProvider([LLMResponse(content=a) for a in answers])
        self.monkeypatch.setattr(session_end, "get_provider", lambda agent, settings=None: provider)
        return provider

    async def passes(self, answer: str) -> None:
        """The two worker passes, with the recap call answering `answer`."""
        self.script_recaps(answer)
        assert await end_silent_sessions(self.factory) >= 1
        # Other tests may leave pending sessions in the shared database.
        assert await write_recaps(self.factory) >= 1

    async def silence(self, conversation_id, answer: str, hours: float = THREE_DAYS) -> None:
        """Hours of silence, then the passes that end the session and write its recap."""
        await self.age(conversation_id, hours)
        await self.passes(answer)

    async def rows(self, conversation_id) -> list[ConversationSession]:
        result = await self.db.execute(
            select(ConversationSession)
            .where(ConversationSession.conversation_id == conversation_id)
            .order_by(ConversationSession.first_seq)
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def contact_id(self, conversation_id) -> uuid.UUID:
        conversation = await self.db.get(Conversation, conversation_id)
        assert conversation is not None
        return conversation.contact_id

    async def memories(self, conversation_id, type_: MemoryType) -> list[Memory]:
        result = await self.db.execute(
            select(Memory)
            .where(
                Memory.contact_id == await self.contact_id(conversation_id), Memory.type == type_
            )
            .order_by(Memory.created_at)
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())


@pytest.fixture
def demo(client, fake_redis, db_session, monkeypatch) -> Demo:
    return Demo(client, fake_redis, db_session, monkeypatch)


def _date(row: ConversationSession) -> str:
    assert row.ended_at is not None
    return row.ended_at.astimezone(UTC).strftime("%d/%m/%Y")


async def test_parts_1_and_2_next_day_then_three_days_of_silence(demo):
    await demo.setup()

    # 1. A message today and one a day later: the second prompt holds the first exchange.
    conversation_id, _ = await demo.chat("primeiro-pedido-alfa", "resposta-alfa")
    await demo.age(conversation_id, 24)
    prompt = (await demo.chat("segundo-pedido-beta", "resposta-beta"))[1]
    assert "primeiro-pedido-alfa" in prompt and "resposta-alfa" in prompt
    assert RECAPS_HEADER not in prompt
    assert [r.status for r in await demo.rows(conversation_id)] == ["open"]

    # 2. Three days of silence, the passes, a new message.
    await demo.silence(
        conversation_id,
        "RECAP: A cliente pediu uma pizza grande e recebeu o prazo.\nADD: A cliente se chama Ana.",
    )
    [old] = await demo.rows(conversation_id)
    assert (old.status, old.ended_by) == ("done", "silence")
    _, prompt = await demo.chat("terceiro-pedido-gama", "resposta-gama")
    for gone in ("primeiro-pedido-alfa", "resposta-alfa", "segundo-pedido-beta", "resposta-beta"):
        assert gone not in prompt
    assert RECAPS_HEADER in prompt
    assert f"{_date(old)}: A cliente pediu uma pizza grande e recebeu o prazo." in prompt
    assert "A cliente se chama Ana." in prompt
    assert "terceiro-pedido-gama" in prompt


async def test_part_3_only_the_three_most_recent_recaps(demo):
    await demo.setup()
    conversation_id, _ = await demo.chat("mensagem-s1", "ok-s1")
    # The first session ended 100 days ago; the next four, three days apart.
    await demo.silence(conversation_id, "RECAP: recap-s1 antigo.", hours=100 * 24)
    for n in (2, 3, 4, 5):
        await demo.chat(f"mensagem-s{n}", f"ok-s{n}")
        await demo.silence(conversation_id, f"RECAP: recap-s{n} recente.")
    _, prompt = await demo.chat("mensagem-s6", "ok-s6")

    # Five ended sessions and the open one: a session the worker already ended
    # is not ended a second time by the next message.
    rows = await demo.rows(conversation_id)
    assert len(rows) == 6
    assert all(r.last_seq is None or r.last_seq >= r.first_seq for r in rows)
    assert len(await demo.memories(conversation_id, MemoryType.SESSION_RECAP)) == 5
    assert RECAPS_HEADER in prompt
    assert "recap-s1" not in prompt  # older than 90 days
    assert "recap-s2" not in prompt  # within 90 days, but not among the three most recent
    assert prompt.index("recap-s3") < prompt.index("recap-s4") < prompt.index("recap-s5")
    for n in (1, 2, 3, 4, 5):
        assert f"mensagem-s{n}" not in prompt


async def test_part_4_a_contradicted_fact_is_replaced_not_duplicated(demo):
    await demo.setup()
    conversation_id, _ = await demo.chat("mensagem-a", "ok-a")
    await demo.silence(conversation_id, "RECAP: Primeira conversa.\nADD: A cliente mora em Recife.")
    _, prompt = await demo.chat("mensagem-b", "ok-b")
    assert "A cliente mora em Recife." in prompt

    await demo.silence(
        conversation_id, "RECAP: Segunda conversa.\nREPLACE 1: A cliente mora em Lisboa."
    )
    _, prompt = await demo.chat("mensagem-c", "ok-c")
    assert "Recife" not in prompt
    assert prompt.count("A cliente mora em Lisboa.") == 1
    tenant_id = (await demo.db.get(Conversation, conversation_id)).tenant_id  # type: ignore[union-attr]
    facts = await MemoryService(demo.db).get_contact_facts(
        tenant_id, await demo.contact_id(conversation_id)
    )
    assert [f.content for f in facts] == ["A cliente mora em Lisboa."]


async def test_part_5_closing_the_conversation_gives_the_same_recap_and_facts(demo):
    await demo.setup()
    answer = "RECAP: A cliente pediu a segunda via.\nADD: A cliente prefere e-mail."

    # Ended by silence: the reference.
    silent_id, _ = await demo.chat("pedido-de-segunda-via", "enviado", user="cust-silent")
    await demo.silence(silent_id, answer)

    # Closed by the app, the same history.
    closed_id, _ = await demo.chat("pedido-de-segunda-via", "enviado", user="cust-closed")
    response = await demo.client.post(f"/conversations/{closed_id}/close", headers=demo.headers)
    assert response.status_code == 200 and response.json()["state"] == "CLOSED"
    [row] = await demo.rows(closed_id)
    closed_date = _date(row)
    assert (row.status, row.ended_by) == ("pending", "closed")
    demo.script_recaps(answer)
    assert await write_recaps(demo.factory) == 1

    [silent_recap] = await demo.memories(silent_id, MemoryType.SESSION_RECAP)
    [closed_recap] = await demo.memories(closed_id, MemoryType.SESSION_RECAP)
    assert closed_recap.content.split(": ", 1)[1] == silent_recap.content.split(": ", 1)[1]
    assert [m.content for m in await demo.memories(closed_id, MemoryType.FACT)] == [
        m.content for m in await demo.memories(silent_id, MemoryType.FACT)
    ]

    # Writing again: a new conversation that holds the recap and the facts.
    new_id, prompt = await demo.chat("voltei", "bem-vinda", user="cust-closed")
    assert new_id != closed_id
    assert RECAPS_HEADER in prompt
    assert f"{closed_date}: A cliente pediu a segunda via." in prompt
    assert "A cliente prefere e-mail." in prompt
    assert "pedido-de-segunda-via" not in prompt


async def _history(demo: Demo, answer: str) -> tuple[uuid.UUID, str]:
    """Eight long exchanges (inside the default window of 20 messages), silence, a return."""
    conversation_id = None
    for n in range(8):
        conversation_id, _ = await demo.chat(
            f"historico-{n}: " + "quero saber do meu pedido e do prazo de entrega. " * 4,
            f"resposta-{n}: " + "seu pedido está a caminho e chega no prazo combinado. " * 4,
        )
    assert conversation_id is not None
    await demo.age(conversation_id, THREE_DAYS)
    demo.script_recaps(answer)
    await end_silent_sessions(demo.factory)
    await write_recaps(demo.factory)
    _, prompt = await demo.chat("voltei", "bem-vinda de volta")
    return conversation_id, prompt


async def test_parts_6_and_7_switched_off_behaves_as_today_and_on_is_smaller(demo, client):
    answer = "RECAP: A cliente perguntou do pedido e do prazo.\nADD: A cliente acompanha pedidos."

    off = await Demo(client, demo.fake_redis, demo.db, demo.monkeypatch).setup(
        session_memory_enabled=False
    )
    off_conversation, off_prompt = await _history(off, answer)
    # 6. The old messages stay, there is no recap section and no session row.
    assert "historico-0" in off_prompt and "resposta-7" in off_prompt
    assert RECAPS_HEADER not in off_prompt
    assert await demo.db.scalar(
        select(func.count()).select_from(ConversationSession).where(
            ConversationSession.conversation_id == off_conversation
        )
    ) == 0
    off_chars = sum(len(str(m.content)) for m in off.messages())

    on = await Demo(client, demo.fake_redis, demo.db, demo.monkeypatch).setup()
    _, on_prompt = await _history(on, answer)
    assert RECAPS_HEADER in on_prompt and "historico-0" not in on_prompt
    on_chars = sum(len(str(m.content)) for m in on.messages())

    # 7. The same history costs fewer characters with the behaviour on.
    print(f"system prompt plus history, characters: on={on_chars} off={off_chars}")
    assert on_chars < off_chars
