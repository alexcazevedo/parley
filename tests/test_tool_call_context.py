"""Every tool call carries who it is for and a call id (ticket 01, call context).

- An HTTP tool called in a turn gets the five `X-Parley-*` headers; its
  arguments (query string or body) are unchanged.
- The `ToolExecution` row holds the same call id and the context record.
- Two calls in one turn have different call ids; the retries of a `GET` inside
  one call share one; a repeated unknown-outcome `GET` carries the first id.
- A contact with no name sends no name header; an accented name is
  percent-encoded (UTF-8).
- A built-in (`remember`) records the context too.
- A call for tenant B never carries tenant A's ids.
- A context header wins over an `auth_config` header of the same name.
"""

import json
import os
import socket
import uuid

import httpx
import pytest
import respx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

import app.tools.http as http_module
from app.agents.runtime import AgentRuntime
from app.conversations import turn as turn_module
from app.conversations.turn import process_turn
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent_run import AgentRun
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.message import Message, MessageDirection, MessageRole
from app.models.tool_execution import ToolExecution, ToolExecutionStatus
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.tools.builtin import BuiltinExecutor
from app.tools.context import CallContext, load_call_context
from app.tools.http import HttpToolExecutor
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from tests.test_tool_reuse import SwitchableAdapter, _earlier_started_run

DEBOUNCE_SECONDS = 0.2
BOOK_URL = "https://example.test/bookings"
ORDERS_URL = "https://example.test/orders"
SCHEMA = {
    "type": "object",
    "properties": {"order_id": {"type": "string"}},
    "required": ["order_id"],
    "additionalProperties": False,
}
ARGS = {"order_id": "12345"}
CONTEXT_HEADERS = (
    "x-parley-conversation-id",
    "x-parley-channel-id",
    "x-parley-user-id",
    "x-parley-contact-name",
    "x-parley-call-id",
)


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    """`example.test` resolves to a public address (feeds the SSRF guard only)."""
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, port, *args, **kwargs):
        if host == "example.test":
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port or 443))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.delenv("TOOL_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("TOOL_SSRF_DISABLED", raising=False)


@pytest.fixture
def adapter(monkeypatch) -> SwitchableAdapter:
    double = SwitchableAdapter()
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: double)
    return double


async def _setup(
    client,
    fake_redis,
    db_session,
    *,
    user_id: str = "user-1",
    contact_name: str | None = None,
    auth_config: dict | None = None,
) -> Conversation:
    """Tenant with a POST `book` and a GET `get_order` tool, one inbound message."""
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    for name, url, method in (("book", BOOK_URL, "POST"), ("get_order", ORDERS_URL, "GET")):
        body = {
            "name": name,
            "description": name,
            "input_schema": SCHEMA,
            "endpoint": url,
            "method": method,
            "timeout_seconds": 5,
        }
        if auth_config is not None:
            body["auth_config"] = auth_config
        created = await client.post("/tools", json=body, headers=headers)
        assert created.status_code == 201, created.text
    created_agent = await client.post(
        "/agents",
        json={
            "name": "Bot",
            "instructions": "Você é o atendente da Acme.",
            "model": "gpt-4o-mini",
            "tool_names": ["book", "get_order"],
        },
        headers=headers,
    )
    assert created_agent.status_code == 201, created_agent.text
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": created_agent.json()["id"]},
        headers=headers,
    )
    assert channel.status_code == 201, channel.text
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel.json()["id"], "user_id": user_id, "message": "cadê?"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None
    conversation = await db_session.get(
        Conversation, uuid.UUID(response.json()["conversation_id"])
    )
    assert conversation is not None
    if contact_name is not None:
        await db_session.execute(
            update(Contact)
            .where(Contact.id == conversation.contact_id)
            .values(display_name=contact_name)
        )
        await db_session.commit()
    return conversation


def _calls(*names: str) -> LLMResponse:
    return LLMResponse(
        tool_calls=[
            ToolCall(id=f"call_{i}", name=name, arguments=dict(ARGS))
            for i, name in enumerate(names)
        ]
    )


def _runtime(provider, executors=None) -> AgentRuntime:
    return AgentRuntime(
        provider=provider, registry=ToolRegistry(executors or [HttpToolExecutor()])
    )


async def _rows(engine, conversation_id) -> list[ToolExecution]:
    async with async_sessionmaker(bind=engine, expire_on_commit=False)() as other:
        runs = select(AgentRun.id).where(AgentRun.conversation_id == conversation_id)
        result = await other.execute(
            select(ToolExecution)
            .where(ToolExecution.agent_run_id.in_(runs))
            .order_by(ToolExecution.created_at)
        )
        return list(result.scalars().all())


async def _inbound(db_session, conversation_id) -> list[Message]:
    result = await db_session.execute(
        select(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.IN,
            Message.role == MessageRole.USER,
        )
        .order_by(Message.seq)
    )
    return list(result.scalars().all())


def _context_headers(request: httpx.Request) -> dict[str, str]:
    return {k: request.headers[k] for k in CONTEXT_HEADERS if k in request.headers}


# --- an HTTP tool in a turn ----------------------------------------------------------


async def test_http_tool_gets_the_five_headers_and_the_row_holds_the_same(
    client, fake_redis, db_session, test_engine
):
    conversation = await _setup(client, fake_redis, db_session, contact_name="Ana")
    provider = FakeLLMProvider([_calls("get_order"), LLMResponse(content="Saiu.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(return_value=httpx.Response(200, json={}))
        await _runtime(provider).run_turn(db_session, conversation, "cadê?")

    [call] = route.calls
    request = call.request
    [row] = await _rows(test_engine, conversation.id)
    assert _context_headers(request) == {
        "x-parley-conversation-id": str(conversation.id),
        "x-parley-channel-id": str(conversation.channel_id),
        "x-parley-user-id": "user-1",
        "x-parley-contact-name": "Ana",
        "x-parley-call-id": str(row.call_id),
    }
    # Arguments unchanged: only the tool's own query string.
    assert dict(request.url.params) == ARGS
    assert row.call_id is not None
    assert row.context == {
        "conversation_id": str(conversation.id),
        "channel_id": str(conversation.channel_id),
        "user_id": "user-1",
        "contact_name": "Ana",
    }


async def test_post_tool_body_is_unchanged(client, fake_redis, db_session, test_engine):
    conversation = await _setup(client, fake_redis, db_session)
    provider = FakeLLMProvider([_calls("book"), LLMResponse(content="Feito.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.post(BOOK_URL).mock(return_value=httpx.Response(200, json={}))
        await _runtime(provider).run_turn(db_session, conversation, "reserva")

    request = route.calls.last.request
    assert json.loads(request.content) == ARGS
    assert "?" not in str(request.url)
    [row] = await _rows(test_engine, conversation.id)
    assert request.headers["x-parley-call-id"] == str(row.call_id)


async def test_two_calls_in_one_turn_have_different_call_ids(
    client, fake_redis, db_session, test_engine
):
    conversation = await _setup(client, fake_redis, db_session)
    provider = FakeLLMProvider([_calls("get_order", "book"), LLMResponse(content="Ok.")])

    async with respx.mock(assert_all_called=False) as router:
        get = router.get(ORDERS_URL).mock(return_value=httpx.Response(200, json={}))
        post = router.post(BOOK_URL).mock(return_value=httpx.Response(200, json={}))
        await _runtime(provider).run_turn(db_session, conversation, "cadê?")

    rows = await _rows(test_engine, conversation.id)
    assert len(rows) == 2
    assert rows[0].call_id != rows[1].call_id
    sent = {
        get.calls.last.request.headers["x-parley-call-id"],
        post.calls.last.request.headers["x-parley-call-id"],
    }
    assert sent == {str(rows[0].call_id), str(rows[1].call_id)}


async def test_get_retries_share_the_call_id(
    client, fake_redis, db_session, test_engine, monkeypatch
):
    async def no_wait(*args, **kwargs):
        return None

    monkeypatch.setattr(http_module, "wait_before_retry", no_wait)
    conversation = await _setup(client, fake_redis, db_session)
    provider = FakeLLMProvider([_calls("get_order"), LLMResponse(content="Saiu.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(
            side_effect=[httpx.Response(503), httpx.Response(200, json={})]
        )
        await _runtime(provider).run_turn(db_session, conversation, "cadê?")

    assert route.call_count == 2
    [row] = await _rows(test_engine, conversation.id)
    ids = {call.request.headers["x-parley-call-id"] for call in route.calls}
    assert ids == {str(row.call_id)}


async def test_repeated_unknown_outcome_get_carries_the_first_call_id(
    client, fake_redis, db_session, test_engine, adapter
):
    conversation = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation.id)
    earlier = await _earlier_started_run(db_session, conversation, messages, "get_order", ARGS)
    first_call_id = uuid.uuid4()
    earlier.call_id = first_call_id
    await db_session.commit()
    provider = FakeLLMProvider([_calls("get_order"), LLMResponse(content="Saiu.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(return_value=httpx.Response(200, json={}))
        await process_turn(db_session, conversation.id, messages, _runtime(provider))

    assert route.call_count == 1
    assert route.calls.last.request.headers["x-parley-call-id"] == str(first_call_id)
    rows = await _rows(test_engine, conversation.id)
    assert [r.status for r in rows] == [ToolExecutionStatus.STARTED, ToolExecutionStatus.OK]
    assert rows[1].call_id == first_call_id


# --- the contact's name -------------------------------------------------------------


async def test_contact_without_name_sends_no_name_header(
    client, fake_redis, db_session, test_engine
):
    conversation = await _setup(client, fake_redis, db_session)
    provider = FakeLLMProvider([_calls("get_order"), LLMResponse(content="Saiu.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(return_value=httpx.Response(200, json={}))
        await _runtime(provider).run_turn(db_session, conversation, "cadê?")

    headers = route.calls.last.request.headers
    assert "x-parley-contact-name" not in headers
    assert "x-parley-user-id" in headers
    [row] = await _rows(test_engine, conversation.id)
    assert "contact_name" not in row.context


async def test_accented_name_is_percent_encoded(client, fake_redis, db_session, test_engine):
    conversation = await _setup(client, fake_redis, db_session, contact_name="José da Silva")
    provider = FakeLLMProvider([_calls("get_order"), LLMResponse(content="Saiu.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(return_value=httpx.Response(200, json={}))
        await _runtime(provider).run_turn(db_session, conversation, "cadê?")

    headers = route.calls.last.request.headers
    assert headers["x-parley-contact-name"] == "Jos%C3%A9%20da%20Silva"
    [row] = await _rows(test_engine, conversation.id)
    assert row.context["contact_name"] == "José da Silva"


# --- built-ins ----------------------------------------------------------------------


async def test_remember_records_the_context(client, fake_redis, db_session, test_engine):
    conversation = await _setup(client, fake_redis, db_session, contact_name="Ana")
    provider = FakeLLMProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(
                        id="call_1",
                        name="remember",
                        arguments={"type": "preference", "content": "prefere PIX"},
                    )
                ]
            ),
            LLMResponse(content="Anotado."),
        ]
    )
    await _runtime(provider, [BuiltinExecutor()]).run_turn(db_session, conversation, "pix")

    [row] = await _rows(test_engine, conversation.id)
    assert row.tool_name == "remember"
    assert row.call_id is not None
    assert row.context == {
        "conversation_id": str(conversation.id),
        "channel_id": str(conversation.channel_id),
        "user_id": "user-1",
        "contact_name": "Ana",
    }


# --- tenants ------------------------------------------------------------------------


async def test_call_for_tenant_b_never_carries_tenant_a_ids(
    client, fake_redis, db_session, test_engine
):
    conv_a = await _setup(client, fake_redis, db_session, user_id="a-user", contact_name="A")
    conv_b = await _setup(client, fake_redis, db_session, user_id="b-user")
    provider = FakeLLMProvider([_calls("get_order"), LLMResponse(content="Saiu.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(return_value=httpx.Response(200, json={}))
        await _runtime(provider).run_turn(db_session, conv_b, "cadê?")

    sent = _context_headers(route.calls.last.request)
    assert sent["x-parley-conversation-id"] == str(conv_b.id)
    assert sent["x-parley-channel-id"] == str(conv_b.channel_id)
    assert sent["x-parley-user-id"] == "b-user"
    assert "x-parley-contact-name" not in sent
    values = " ".join(sent.values())
    for leaked in (str(conv_a.id), str(conv_a.channel_id), "a-user"):
        assert leaked not in values
    [row] = await _rows(test_engine, conv_b.id)
    assert row.tenant_id == conv_b.tenant_id
    assert "a-user" not in str(row.context)

    # The loader is tenant-scoped: B's conversation is not visible from A.
    with pytest.raises(LookupError):
        await load_call_context(db_session, conv_a.tenant_id, conv_b.id)


# --- auth header collision ----------------------------------------------------------


async def test_context_header_wins_over_an_auth_header_of_the_same_name(
    client, fake_redis, db_session, test_engine
):
    conversation = await _setup(
        client,
        fake_redis,
        db_session,
        auth_config={"type": "header", "token": "secret", "header_name": "x-parley-user-id"},
    )
    provider = FakeLLMProvider([_calls("get_order"), LLMResponse(content="Saiu.")])

    async with respx.mock(assert_all_called=False) as router:
        route = router.get(ORDERS_URL).mock(return_value=httpx.Response(200, json={}))
        await _runtime(provider).run_turn(db_session, conversation, "cadê?")

    request = route.calls.last.request
    assert request.headers.get_list("x-parley-user-id") == ["user-1"]


# --- CallContext --------------------------------------------------------------------


def test_call_context_record_headers_and_meta():
    conversation_id, channel_id, call_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    call = CallContext(conversation_id, channel_id, "u-1", "Zoë")
    assert call.as_record() == {
        "conversation_id": str(conversation_id),
        "channel_id": str(channel_id),
        "user_id": "u-1",
        "contact_name": "Zoë",
    }
    assert call.as_headers(call_id) == {
        "X-Parley-Conversation-Id": str(conversation_id),
        "X-Parley-Channel-Id": str(channel_id),
        "X-Parley-User-Id": "u-1",
        "X-Parley-Contact-Name": "Zo%C3%AB",
        "X-Parley-Call-Id": str(call_id),
    }
    assert call.as_meta(call_id) == {
        "parley/conversation_id": str(conversation_id),
        "parley/channel_id": str(channel_id),
        "parley/user_id": "u-1",
        "parley/contact_name": "Zoë",
        "parley/call_id": str(call_id),
    }
    nameless = CallContext(conversation_id, channel_id, "u-1", None)
    assert "contact_name" not in nameless.as_record()
    assert "X-Parley-Contact-Name" not in nameless.as_headers(call_id)
    assert "parley/contact_name" not in nameless.as_meta(call_id)
