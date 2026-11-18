"""A turn holds no database transaction across slow calls (PRODUCT.md E3.6).

Every fake below records `session.in_transaction()` at the moment the turn
calls it: the channel adapter (endpoint check, read receipt, typing, send),
speech-to-text (a respx route), the pacing `asyncio.sleep` and the summary
model. Every recorded value must be `False`.

Reply rows follow the send: a failed send leaves no row, a successful one is
stored right after it.
"""

import os
import uuid
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select

from app.config import get_settings
from app.conversations import turn as turn_module
from app.conversations.turn import UNSUPPORTED_MEDIA_REPLY, TurnFailed, process_turn
from app.db import end_transaction
from app.llm.base import LLMResponse
from app.models.agent import Agent
from app.models.conversation import Conversation, ConversationState
from app.models.message import Message, MessageDirection, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.workers.main import run_once

DEBOUNCE_SECONDS = 0.2
FIXTURES = Path(__file__).parent / "fixtures"
TRANSCRIPT = "quando chega meu pedido?"
MULTI_PART_REPLY = (
    "Oi! Achei seu pedido aqui.\n\n"
    "Ele saiu para entrega hoje de manhã.\n\n"
    "Quer que eu te avise quando o entregador chegar?"
)
PARTS = MULTI_PART_REPLY.split("\n\n")


class SendFailed(Exception):
    pass


class RecordingAdapter:
    """Records `(call, session.in_transaction())` for every adapter call.

    `send_message` also records how many assistant rows the turn's session
    sees at that moment, *after* noting the transaction state (the count query
    opens a transaction of its own, closed again before returning).
    """

    def __init__(self, session, conversation_id: uuid.UUID, *, fail_send: bool = False) -> None:
        self.session = session
        self.conversation_id = conversation_id
        self.fail_send = fail_send
        self.calls: list[tuple[str, bool]] = []
        self.rows_at_send: list[int] = []

    def parse_incoming(self, payload):  # pragma: no cover - unused here
        raise NotImplementedError

    async def check_endpoint(self, channel) -> None:
        self.calls.append(("check_endpoint", self.session.in_transaction()))

    async def mark_read(self, external_message_id, contact, channel) -> None:
        self.calls.append(("mark_read", self.session.in_transaction()))

    async def send_typing(self, contact, channel, duration_seconds) -> None:
        self.calls.append(("typing", self.session.in_transaction()))

    async def send_message(self, message, contact, channel) -> None:
        self.calls.append(("send", self.session.in_transaction()))
        self.rows_at_send.append(len(await _replies(self.session, self.conversation_id)))
        # The count query opened a transaction: close it so the next recorded
        # call sees only what the turn itself holds. (A rollback would expire
        # the turn's loaded rows.)
        await self.session.commit()
        if self.fail_send:
            raise SendFailed("provider returned 503")


class RecordingProvider:
    """Summary model: records the transaction state at each `complete`."""

    def __init__(self, session) -> None:
        self.session = session
        self.in_transaction: list[bool] = []

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        self.in_transaction.append(self.session.in_transaction())
        return LLMResponse(content="Resumo da conversa")


class SummarizingRuntime:
    """Text runtime that exposes a summary provider, like `AgentRuntime` does."""

    def __init__(self, provider: RecordingProvider, reply: str = "ok") -> None:
        self.provider = provider
        self.reply = reply

    async def run(self, conversation, input_text: str) -> str:
        return self.reply

    def llm_provider(self, agent):
        return self.provider


class TextRuntime:
    def __init__(self, reply: str = "ok") -> None:
        self.reply = reply

    async def run(self, conversation, input_text: str) -> str:
        return self.reply


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def _setup(client, *, humanize: bool = False, summary_threshold: int = 0):
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    agent = await client.post(
        "/agents",
        json={
            "name": "Bot",
            "instructions": "Hi.",
            "model": "gpt-4o-mini",
            "summary_threshold": summary_threshold,
        },
        headers=headers,
    )
    assert agent.status_code == 201, agent.text
    channel = await client.post(
        "/channels",
        json={
            "type": "webchat",
            "name": "Web",
            "agent_id": agent.json()["id"],
            "config": {"humanize": True} if humanize else {},
        },
        headers=headers,
    )
    assert channel.status_code == 201, channel.text
    return headers, channel.json()["id"], uuid.UUID(agent.json()["id"])


async def _send_text(client, headers, channel_id, text: str = "Oi") -> uuid.UUID:
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "user-1", "message": text},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["conversation_id"])


async def _send_audio(client, headers, channel_id) -> uuid.UUID:
    response = await client.post(
        "/channels/webchat/media",
        data={"channel_id": str(channel_id), "user_id": "user-1"},
        files={
            "file": ("voice_note.mp3", (FIXTURES / "voice_note.mp3").read_bytes(), "audio/mpeg")
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["conversation_id"])


async def _drain_jobs(fake_redis) -> None:
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None


async def _inbound(session, conversation_id) -> list[Message]:
    result = await session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.IN,
        )
        .order_by(Message.created_at)
    )
    return list(result.scalars().all())


async def _replies(session, conversation_id) -> list[str]:
    result = await session.execute(
        select(Message.content)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.OUT,
            Message.role == MessageRole.ASSISTANT,
        )
        .order_by(Message.created_at)
    )
    return list(result.scalars().all())


def _stub_adapter(monkeypatch, adapter: RecordingAdapter) -> RecordingAdapter:
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: adapter)
    return adapter


def _recording_sleep(monkeypatch, session) -> list[bool]:
    recorded: list[bool] = []

    async def fake_sleep(seconds: float) -> None:
        recorded.append(session.in_transaction())

    monkeypatch.setattr(turn_module.asyncio, "sleep", fake_sleep)
    return recorded


async def _turn(db_session, conversation_id, runtime):
    messages = await _inbound(db_session, conversation_id)
    assert db_session.in_transaction()  # the turn starts inside one, like the worker's
    return await process_turn(db_session, conversation_id, messages, runtime)


# --- end_transaction -----------------------------------------------------------


async def test_end_transaction_commits_an_open_one_and_is_a_noop_otherwise(db_session):
    await end_transaction(db_session)  # nothing open: nothing happens
    assert not db_session.in_transaction()

    await db_session.execute(select(1))
    assert db_session.in_transaction()
    await end_transaction(db_session)
    assert not db_session.in_transaction()


# --- single reply --------------------------------------------------------------


async def test_single_reply_turn_holds_no_transaction_across_slow_calls(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id, _ = await _setup(client, summary_threshold=1)
    conversation_id = await _send_text(client, headers, channel_id)
    await _drain_jobs(fake_redis)
    adapter = _stub_adapter(monkeypatch, RecordingAdapter(db_session, conversation_id))
    provider = RecordingProvider(db_session)

    reply = await _turn(db_session, conversation_id, SummarizingRuntime(provider))

    assert isinstance(reply, Message) and reply.content == "ok"
    assert adapter.calls == [("check_endpoint", False), ("send", False)]
    assert provider.in_transaction == [False]
    # Sent first, stored right after.
    assert adapter.rows_at_send == [0]
    assert await _replies(db_session, conversation_id) == ["ok"]
    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation.summary == "Resumo da conversa"


async def test_failed_send_leaves_no_reply_row(client, fake_redis, db_session, monkeypatch):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send_text(client, headers, channel_id)
    await _drain_jobs(fake_redis)
    adapter = _stub_adapter(
        monkeypatch, RecordingAdapter(db_session, conversation_id, fail_send=True)
    )

    with pytest.raises(TurnFailed) as caught:
        await _turn(db_session, conversation_id, TextRuntime())

    assert caught.value.delivered_parts == 0
    assert adapter.calls == [("check_endpoint", False), ("send", False)]
    assert adapter.rows_at_send == [0]
    assert await _replies(db_session, conversation_id) == []


# --- humanized -----------------------------------------------------------------


async def test_humanized_turn_holds_no_transaction_across_slow_calls(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id, _ = await _setup(client, humanize=True)
    conversation_id = await _send_text(client, headers, channel_id)
    await _drain_jobs(fake_redis)
    adapter = _stub_adapter(monkeypatch, RecordingAdapter(db_session, conversation_id))
    sleeps = _recording_sleep(monkeypatch, db_session)

    await _turn(db_session, conversation_id, TextRuntime(MULTI_PART_REPLY))

    assert [name for name, _ in adapter.calls] == [
        "check_endpoint",
        "mark_read",
        *(["typing", "send"] * 3),
    ]
    assert all(state is False for _, state in adapter.calls), adapter.calls
    assert sleeps == [False, False, False]
    # Each part is stored after its own send, before the next one.
    assert adapter.rows_at_send == [0, 1, 2]
    assert await _replies(db_session, conversation_id) == PARTS


async def test_humanized_failed_send_stores_only_the_parts_sent(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id, _ = await _setup(client, humanize=True)
    conversation_id = await _send_text(client, headers, channel_id)
    await _drain_jobs(fake_redis)

    class FailSecond(RecordingAdapter):
        async def send_message(self, message, contact, channel) -> None:
            self.fail_send = len(self.rows_at_send) == 1
            await super().send_message(message, contact, channel)

    adapter = _stub_adapter(monkeypatch, FailSecond(db_session, conversation_id))
    _recording_sleep(monkeypatch, db_session)

    with pytest.raises(TurnFailed) as caught:
        await _turn(db_session, conversation_id, TextRuntime(MULTI_PART_REPLY))

    assert caught.value.delivered_parts == 1
    assert adapter.rows_at_send == [0, 1]
    assert await _replies(db_session, conversation_id) == [PARTS[0]]


# --- speech-to-text --------------------------------------------------------------


async def test_transcription_runs_outside_a_transaction(
    client, fake_redis, db_session, monkeypatch, media_root
):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send_audio(client, headers, channel_id)
    await _drain_jobs(fake_redis)
    adapter = _stub_adapter(monkeypatch, RecordingAdapter(db_session, conversation_id))
    stt_states: list[bool] = []

    def transcribe(request):
        stt_states.append(db_session.in_transaction())
        return httpx.Response(200, json={"text": TRANSCRIPT})

    async with respx.mock(assert_all_called=False) as router:
        router.post("https://api.openai.com/v1/audio/transcriptions").mock(side_effect=transcribe)
        await _turn(db_session, conversation_id, TextRuntime())

    assert stt_states == [False]
    assert adapter.calls == [("check_endpoint", False), ("send", False)]
    [inbound] = await _inbound(db_session, conversation_id)
    assert inbound.content == TRANSCRIPT


# --- fixed replies ---------------------------------------------------------------


async def test_unsupported_media_reply_is_sent_outside_a_transaction(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send_text(client, headers, channel_id)
    await _drain_jobs(fake_redis)
    for message in await _inbound(db_session, conversation_id):
        message.type = "unsupported"
    await db_session.commit()
    adapter = _stub_adapter(monkeypatch, RecordingAdapter(db_session, conversation_id))

    reply = await _turn(db_session, conversation_id, TextRuntime())

    assert isinstance(reply, Message) and reply.content == UNSUPPORTED_MEDIA_REPLY
    assert adapter.calls == [("check_endpoint", False), ("send", False)]
    assert adapter.rows_at_send == [0]
    assert await _replies(db_session, conversation_id) == [UNSUPPORTED_MEDIA_REPLY]


async def test_handoff_reply_is_sent_outside_a_transaction(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id, agent_id = await _setup(client)
    conversation_id = await _send_text(client, headers, channel_id)
    await _drain_jobs(fake_redis)
    agent = await db_session.get(Agent, agent_id)
    agent.sentiment_escalation_enabled = True
    await db_session.commit()

    async def frustrated(session, conversation, agent, input_text):
        return 1.0

    async def handoff(session, conversation, *, reason, requested_by):
        conversation.state = ConversationState.HUMAN_HANDOFF

    monkeypatch.setattr(turn_module, "score_frustration", frustrated)
    monkeypatch.setattr(turn_module, "handoff_with_summary", handoff)
    adapter = _stub_adapter(monkeypatch, RecordingAdapter(db_session, conversation_id))

    reply = await _turn(db_session, conversation_id, TextRuntime())

    assert isinstance(reply, Message) and reply.content == agent.handoff_message
    assert adapter.calls == [("check_endpoint", False), ("send", False)]
    assert adapter.rows_at_send == [0]
    assert await _replies(db_session, conversation_id) == [agent.handoff_message]
