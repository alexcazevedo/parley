"""PRODUCT.md success demo of the conversation-correctness run, part 2.

One test per clause, through public entry points (the Evolution webhook,
`process_turn`, the scheduler tick, the API client). The migration clause is
proven by `tests/test_migration_message_seq.py` and the network clause by
`tests/test_no_network.py`.
"""

# ruff: noqa: F811  (the imported `adapter` fixture is used as a test argument)

import json
from datetime import timedelta

import httpx
import pytest
import respx
from sqlalchemy import select

import app.agents.runtime as runtime_module
import app.agents.sentiment as sentiment_module
from app.agents.runtime import AgentRuntime
from app.conversations import turn as turn_module
from app.conversations.summary import SUMMARIZER_SYSTEM_PROMPT
from app.conversations.turn import process_turn
from app.llm.base import ROLE_ASSISTANT, ROLE_USER, ImagePart, LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import DEFAULT_HANDOFF_MESSAGE
from app.models.conversation import Conversation
from app.models.message import Message, MessageRole
from app.queue.redis_queue import JOBS_KEY
from app.tools.registry import ToolRegistry
from tests.test_api_errors import _evolution_channel, _tenant
from tests.test_context_limit import _add, _tool_step
from tests.test_evolution import _auth, _create_evolution_channel, _create_tenant
from tests.test_handoff_default_message import _clear_handoff_message
from tests.test_handoff_reply_owed import (  # noqa: F401  (adapter is a fixture)
    _failed_first_attempt,
    _handoff_runtime,
    _inbound,
    _silent_runtime,
    _stored_replies,
    adapter,
)
from tests.test_handoff_reply_owed import (
    _setup as _owed_setup,
)
from tests.test_message_order import _setup as _webchat_setup
from tests.test_summary import _drain_jobs, _run_turn, _send
from tests.test_webhook_fast import (
    APIKEY,
    GET_BASE64_URL,
    SEND_TEXT_URL,
    _fixture,
    _media_response,
    _png,
    _rows,
    media_root,  # noqa: F401  (fixture)
)

HOUR = timedelta(hours=1)


def _skew_reply_clock(monkeypatch, skew: timedelta) -> None:
    """Every `datetime.now` that stamps a reply runs `skew` off the database's."""
    from datetime import datetime as real

    class Skewed(real):
        @classmethod
        def now(cls, tz=None):
            return real.now(tz) + skew

    monkeypatch.setattr(turn_module, "datetime", Skewed)
    monkeypatch.setattr(runtime_module, "datetime", Skewed)


# --- a repeated WhatsApp delivery causes no download and no second message ---------


@respx.mock
async def test_repeated_delivery_causes_no_download_and_no_second_message(
    client, fake_redis, db_session, media_root
):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    route = respx.post(GET_BASE64_URL).mock(return_value=_media_response(_png(), "image/png"))
    payload = _fixture("evolution_image_upsert.json")

    first = await client.post(f"/webhooks/whatsapp/{channel_id}", json=payload, headers=APIKEY)
    second = await client.post(f"/webhooks/whatsapp/{channel_id}", json=payload, headers=APIKEY)

    assert first.json() == second.json() == {"status": "ok"}
    assert route.call_count == 0
    assert len(await _rows(db_session, channel_id)) == 1
    assert await fake_redis.llen(JOBS_KEY) == 1


# --- acknowledged before the download, still understood by the turn ----------------


@respx.mock
async def test_media_is_acknowledged_before_download_and_understood_by_the_turn(
    client, fake_redis, db_session, media_root
):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    route = respx.post(GET_BASE64_URL).mock(return_value=_media_response(_png(), "image/png"))
    respx.post(SEND_TEXT_URL).mock(return_value=httpx.Response(200, json={}))

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_fixture("evolution_image_upsert.json"),
        headers=APIKEY,
    )
    assert response.json() == {"status": "ok"}
    assert route.call_count == 0  # answered, nothing downloaded yet

    await _drain_jobs(fake_redis)
    provider = FakeLLMProvider([LLMResponse(content="Vi a etiqueta.")])
    await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    assert route.call_count == 1  # downloaded by the turn
    (parts,) = [m.content for m in provider.calls[0]["messages"] if m.role == ROLE_USER]
    assert [i.data for i in parts if isinstance(i, ImagePart)] == [_png()]
    rows = await _rows(db_session, channel_id)
    assert rows[-1].content == "Vi a etiqueta."


# --- a stored event contains no base64 ---------------------------------------------


async def test_stored_event_contains_no_base64(client, fake_redis, db_session, media_root):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    payload = _fixture("evolution_image_upsert.json")
    payload["data"]["message"]["base64"] = "QUFBQQ=="
    payload["data"]["message"]["imageMessage"]["jpegThumbnail"] = "QkJCQg=="

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}", json=payload, headers=APIKEY
    )
    assert response.json() == {"status": "ok"}

    (row,) = await _rows(db_session, channel_id)
    stored = json.dumps(row.raw_payload)
    assert "QUFBQQ==" not in stored and "QkJCQg==" not in stored
    assert '"base64"' not in stored and '"jpegThumbnail"' not in stored


# --- the sentiment prompt contains the batch once ----------------------------------


class _Capturing:
    def __init__(self) -> None:
        self.prompts: list = []

    async def complete(self, messages, tools=None, *, model, temperature=0.7, max_tokens=1024):
        self.prompts.append(messages)
        return LLMResponse(content="0.1")


class _PlainRuntime:
    async def run(self, conversation, input_text: str) -> str:
        return "Claro."


async def test_sentiment_prompt_contains_the_batch_once(
    client, fake_redis, db_session, adapter, monkeypatch
):
    _, _, conversation_id = await _owed_setup(client, fake_redis, db_session, sentiment=True)
    provider = _Capturing()
    monkeypatch.setattr(sentiment_module, "get_provider", lambda agent, settings=None: provider)
    messages = await _inbound(db_session, conversation_id)

    await process_turn(db_session, conversation_id, messages, _PlainRuntime())

    (prompt,) = provider.prompts
    assert prompt[1].content.count("quero uma pessoa") == 1


# --- order and rolling summary under a skewed clock --------------------------------


@pytest.mark.parametrize("skew", [HOUR, -HOUR], ids=["clock_ahead", "clock_behind"])
async def test_replies_stamped_by_a_skewed_clock_are_read_in_order_and_summarised_once(
    client, fake_redis, db_session, monkeypatch, skew
):
    _skew_reply_clock(monkeypatch, skew)
    headers, channel_id, _ = await _webchat_setup(client, summary_threshold=2)
    rounds = 3
    script = []
    for n in range(1, rounds + 1):
        script += [LLMResponse(content=f"reply-{n}"), LLMResponse(content=f"summary-{n}")]
    provider = FakeLLMProvider(script)
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([]))

    for n in range(1, rounds + 1):
        conversation_id = await _send(client, headers, channel_id, f"user-{n}")
        await _drain_jobs(fake_redis)
        await _run_turn(fake_redis, db_session, runtime)

    expected = [text for n in range(1, rounds + 1) for text in (f"user-{n}", f"reply-{n}")]

    # The operator's view and the model's view agree with the order it happened.
    listed = await client.get(f"/conversations/{conversation_id}/messages", headers=headers)
    assert [m["content"] for m in listed.json()] == expected
    last_turn_prompt = [
        m.content
        for m in provider.calls[-2]["messages"]
        if m.role in (ROLE_USER, ROLE_ASSISTANT) and m.content
    ]
    assert last_turn_prompt == expected[:-1]

    # The summary covered every message exactly once across the rounds.
    summarizer_prompts = [
        call["messages"][-1].content
        for call in provider.calls
        if call["messages"][0].content == SUMMARIZER_SYSTEM_PROMPT
    ]
    assert len(summarizer_prompts) == rounds
    lines = [line for prompt in summarizer_prompts for line in prompt.splitlines()]
    for content in expected:
        assert sum(line.endswith(f": {content}") for line in lines) == 1, content
    last_seq = await db_session.scalar(
        select(Message.seq)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.seq.desc())
        .limit(1)
    )
    db_session.expire_all()
    conversation = await db_session.get(Conversation, conversation_id)
    assert conversation.summary_last_seq == last_seq


# --- a history limit of N gives N customer and assistant messages, tools intact ----


async def test_history_limit_gives_n_conversation_messages_with_tool_rows_intact(
    client, fake_redis, db_session
):
    headers, channel_id, agent_id = await _webchat_setup(client, context_message_limit=3)
    conversation_id = await _send(client, headers, channel_id, "m0")
    await _drain_jobs(fake_redis)
    provider = FakeLLMProvider([LLMResponse(content="r0"), LLMResponse(content="r-final")])
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([]))
    await _run_turn(fake_redis, db_session, runtime)

    conversation = await db_session.get(Conversation, conversation_id)
    await _add(db_session, conversation, MessageRole.USER, "u2")
    await _tool_step(db_session, conversation, "t1", 2)
    await _add(db_session, conversation, MessageRole.ASSISTANT, "a3")
    await db_session.commit()

    await _send(client, headers, channel_id, "final")
    await _drain_jobs(fake_redis)
    await _run_turn(fake_redis, db_session, runtime)

    history = provider.calls[1]["messages"][1:]  # without the system prompt
    counted = [
        m for m in history if m.role == ROLE_USER or (m.role == ROLE_ASSISTANT and not m.tool_calls)
    ]
    assert [m.content for m in counted] == ["u2", "a3", "final"]
    # The tool call of "u2"'s turn is not cut off from its results.
    (call_message,) = [m for m in history if m.tool_calls]
    results = [m for m in history if m.role == "tool"]
    assert {c.id for c in call_message.tool_calls} == {m.tool_call_id for m in results}
    assert len(results) == 2
    assert history.index(call_message) < min(history.index(m) for m in results)


# --- a webchat call on a WhatsApp channel is a 4xx ---------------------------------


async def test_webchat_call_on_a_whatsapp_channel_returns_422(client):
    headers = await _tenant(client)
    channel_id = await _evolution_channel(client, headers)

    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "u", "message": "hi"},
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "channel is not a webchat channel"


# --- a validation failure is 422 in the error envelope -----------------------------


async def test_validation_failure_returns_422_in_the_error_envelope(client):
    headers = await _tenant(client)

    response = await client.post("/channels", json={"type": "webchat"}, headers=headers)

    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert error["request_id"]
    assert {"field": "name", "message": "Field required"} in error["details"]


# --- a retried handoff message with no configured text sends the default -----------


async def test_retried_handoff_message_without_configured_text_sends_the_default(
    client, fake_redis, db_session, test_engine, adapter
):
    _, _, conversation_id = await _owed_setup(client, fake_redis, db_session)
    await _clear_handoff_message(db_session, conversation_id)
    messages = await _inbound(db_session, conversation_id)
    await _failed_first_attempt(db_session, conversation_id, messages, adapter, _handoff_runtime())

    await process_turn(db_session, conversation_id, messages, _silent_runtime())

    assert adapter.sent == [DEFAULT_HANDOFF_MESSAGE]
    assert await _stored_replies(test_engine, conversation_id) == [DEFAULT_HANDOFF_MESSAGE]
