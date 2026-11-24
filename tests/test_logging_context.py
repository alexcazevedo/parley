"""Log context (E7): request id check, turn and run context, no stdlib logging."""

# ruff: noqa: F811  (the imported `adapter` fixture is used as a test argument)

import re
import uuid
from pathlib import Path

import pytest
import structlog
from structlog.contextvars import bind_contextvars, get_contextvars

from app.agents.runtime import AgentRuntime
from app.conversations.turn import TurnFailed, process_turn
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.conversation import Conversation
from app.tools.builtin import BuiltinExecutor
from app.tools.registry import ToolRegistry
from tests.test_handoff_reply_owed import _inbound, _setup, adapter  # noqa: F401

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
KEYS = ("tenant_id", "conversation_id", "run_id")


def _worker_keys() -> dict:
    return {k: v for k, v in get_contextvars().items() if k in KEYS}


class SpyProvider(FakeLLMProvider):
    """Records the bound context at the moment the model is called."""

    def __init__(self) -> None:
        super().__init__([LLMResponse(content="ok")])
        self.seen: list[dict] = []

    async def complete(self, *args, **kwargs) -> LLMResponse:
        self.seen.append(dict(get_contextvars()))
        return await super().complete(*args, **kwargs)


def _runtime(provider) -> AgentRuntime:
    return AgentRuntime(provider=provider, registry=ToolRegistry([BuiltinExecutor()]))


# --- request id -------------------------------------------------------------


async def test_well_formed_request_id_is_echoed(client):
    response = await client.get("/health", headers={"X-Request-ID": "abc-123"})
    assert response.headers["X-Request-ID"] == "abc-123"


@pytest.mark.parametrize(
    "bad", ["x" * 65, "has space", "line\nbreak", "tab\there", "ação", ""], ids=repr
)
async def test_malformed_request_id_is_replaced(client, bad):
    try:
        response = await client.post(
            "/channels/webchat/messages",
            json={"channel_id": str(uuid.uuid4()), "user_id": "u", "message": "hi"},
            headers={"X-Request-ID": bad},
        )
    except Exception:  # httpx refuses some header values itself
        pytest.skip("header value cannot be sent by the client")
    header = response.headers["X-Request-ID"]
    assert header != bad
    assert UUID_RE.match(header)
    assert response.json()["error"]["request_id"] == header


async def test_64_char_request_id_is_kept(client):
    value = "a" * 64
    response = await client.get("/health", headers={"X-Request-ID": value})
    assert response.headers["X-Request-ID"] == value


# --- turn and run context ---------------------------------------------------


async def test_turn_and_run_context_is_bound_and_cleared(
    client, fake_redis, db_session, adapter
):
    _, _, conversation_id = await _setup(client, fake_redis, db_session)
    conversation = await db_session.get(Conversation, conversation_id)
    tenant_id = str(conversation.tenant_id)
    messages = await _inbound(db_session, conversation_id)
    provider = SpyProvider()
    bind_contextvars(request_id="req-1")
    try:
        with structlog.testing.capture_logs(
            processors=[structlog.contextvars.merge_contextvars]
        ) as logs:
            await process_turn(db_session, conversation_id, messages, _runtime(provider))

        [seen] = provider.seen
        assert seen["tenant_id"] == tenant_id
        assert seen["conversation_id"] == str(conversation_id)
        assert UUID_RE.match(seen["run_id"])
        finished = [e for e in logs if e["event"] == "agent.run_finished"]
        assert finished
        assert finished[0]["tenant_id"] == tenant_id
        assert finished[0]["run_id"] == finished[0]["agent_run_id"]
        assert _worker_keys() == {}
        assert get_contextvars()["request_id"] == "req-1"
    finally:
        structlog.contextvars.clear_contextvars()


async def test_context_is_cleared_when_the_turn_fails(
    client, fake_redis, db_session, adapter
):
    _, _, conversation_id = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation_id)
    adapter.fail = True
    with pytest.raises(TurnFailed):
        await process_turn(db_session, conversation_id, messages, _runtime(SpyProvider()))
    assert _worker_keys() == {}


async def test_prebound_keys_are_restored_after_the_turn(
    client, fake_redis, db_session, adapter
):
    _, _, conversation_id = await _setup(client, fake_redis, db_session)
    messages = await _inbound(db_session, conversation_id)
    bind_contextvars(conversation_id="outer", tenant_id="outer-t", run_id="outer-r")
    try:
        await process_turn(db_session, conversation_id, messages, _runtime(SpyProvider()))
        assert _worker_keys() == {
            "conversation_id": "outer",
            "tenant_id": "outer-t",
            "run_id": "outer-r",
        }
    finally:
        structlog.contextvars.clear_contextvars()


# --- one logging library ----------------------------------------------------


def test_only_the_logging_module_imports_stdlib_logging():
    root = Path(__file__).parent.parent / "app"
    offenders = [
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if re.search(r"^import logging\b", p.read_text(), re.M)
    ]
    assert offenders == ["observability/logging.py"]


async def test_webhook_events_are_structlog_events(client, fake_redis):
    from tests.test_evolution import _auth, _create_evolution_channel, _create_tenant

    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    with structlog.testing.capture_logs() as logs:
        response = await client.post(
            f"/webhooks/whatsapp/{channel_id}",
            json={"event": "connection.update", "data": {}},
            headers={"apikey": "evo-secret-xyz"},
        )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert any(e["event"] == "webhook.event_ignored" for e in logs)
