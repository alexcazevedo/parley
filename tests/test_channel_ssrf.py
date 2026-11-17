"""PRODUCT.md F1: an Evolution channel's `base_url` is tenant-controlled, so
the server must only call public addresses with it — checked at create, at
update and on every outbound call — unless the operator listed the host in
`CHANNEL_ALLOWED_HOSTS`. `TOOL_ALLOWED_HOSTS` / `TOOL_SSRF_DISABLED` never
apply to channels.

DNS: `tests/conftest.py::_stable_dns` makes `*.example.com` resolve to a
public address; tests that need another answer patch `socket.getaddrinfo`.
"""

import socket
import uuid
from pathlib import Path

import httpx
import pytest
import respx
from sqlalchemy import select
from structlog.testing import capture_logs

from app.agents.runtime import AgentRuntime
from app.channels.base import IncomingMessage, OutgoingMessage
from app.channels.evolution import EvolutionAdapter
from app.config import get_settings
from app.conversations.turn import process_turn
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent_run import AgentRun
from app.models.channel import Channel
from app.models.contact import Contact
from app.models.message import Message, MessageDirection
from app.tools.ssrf import (
    SSRFError,
    assert_safe_channel_url,
    assert_safe_url,
    validate_channel_base_url,
    validate_tool_endpoint,
)
from tests.test_evolution import _auth, _create_evolution_channel, _create_tenant

FIXTURES = Path(__file__).parent / "fixtures"
PRIVATE_URL = "http://10.0.0.5:8080"
APIKEY = "evo-secret-xyz"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("CHANNEL_ALLOWED_HOSTS", "TOOL_ALLOWED_HOSTS", "TOOL_SSRF_DISABLED"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _set_env(monkeypatch, name: str, value: str) -> None:
    monkeypatch.setenv(name, value)
    get_settings.cache_clear()


async def _create_agent(client, headers: dict) -> str:
    agent = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Hi.", "model": "gpt-4o-mini"},
        headers=headers,
    )
    assert agent.status_code == 201
    return agent.json()["id"]


async def _post_channel(client, headers: dict, base_url: str):
    agent_id = await _create_agent(client, headers)
    return await client.post(
        "/channels",
        json={
            "type": "whatsapp_evolution",
            "name": "WhatsApp",
            "agent_id": agent_id,
            "config": {"base_url": base_url, "instance": "harness-instance"},
            "secret": APIKEY,
        },
        headers=headers,
    )


async def _channel_rows(db_session, tenant_id: str) -> list[Channel]:
    result = await db_session.execute(
        select(Channel).where(Channel.tenant_id == uuid.UUID(tenant_id))
    )
    return list(result.scalars().all())


async def _force_base_url(db_session, channel_id: str, base_url: str) -> Channel:
    """Write a `base_url` straight into the row, as an older version could have."""
    channel = await db_session.get(Channel, uuid.UUID(channel_id))
    channel.config = {**channel.config, "base_url": base_url}
    await db_session.commit()
    return channel


# --- create / update -------------------------------------------------------


async def test_create_refuses_private_base_url(client, db_session):
    tenant = await _create_tenant(client)
    response = await _post_channel(client, _auth(tenant["api_key"]), PRIVATE_URL)

    assert response.status_code == 422
    assert response.json()["error"]["message"].startswith("invalid base_url: ")
    assert "10.0.0.5" in response.json()["error"]["message"]
    assert await _channel_rows(db_session, tenant["id"]) == []


async def test_update_refuses_private_base_url(client, db_session):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    response = await client.patch(
        f"/channels/{channel_id}",
        json={"config": {"base_url": PRIVATE_URL, "instance": "harness-instance"}},
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["message"].startswith("invalid base_url: ")
    db_session.expire_all()
    (row,) = await _channel_rows(db_session, tenant["id"])
    assert row.config["base_url"] == "https://evolution.example.com"


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080",
        "http://169.254.169.254",
        "http://localhost:8080",
        "http://evolution.internal",
        "http://0x7f000001",
        "http://[::1]:8080",
        "file:///etc/passwd",
    ],
)
async def test_create_refuses_other_internal_targets(client, url):
    tenant = await _create_tenant(client)
    response = await _post_channel(client, _auth(tenant["api_key"]), url)
    assert response.status_code == 422
    assert response.json()["error"]["message"].startswith("invalid base_url: ")


async def test_update_without_config_does_not_recheck(client, db_session):
    """A rename must not be blocked by (or repair) a stored bad row."""
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)
    await _force_base_url(db_session, channel_id, PRIVATE_URL)

    response = await client.patch(
        f"/channels/{channel_id}", json={"name": "Renamed"}, headers=headers
    )
    assert response.status_code == 200


async def test_listed_host_is_accepted_at_create_and_update(client, monkeypatch):
    _set_env(monkeypatch, "CHANNEL_ALLOWED_HOSTS", "evo.lan, 10.0.0.5")
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])

    created = await _post_channel(client, headers, PRIVATE_URL)
    assert created.status_code == 201

    channel_id = await _create_evolution_channel(client, headers)
    updated = await client.patch(
        f"/channels/{channel_id}",
        json={"config": {"base_url": PRIVATE_URL, "instance": "harness-instance"}},
        headers=headers,
    )
    assert updated.status_code == 200
    assert updated.json()["config"]["base_url"] == PRIVATE_URL


async def test_list_is_additive_other_hosts_still_checked(client, monkeypatch):
    _set_env(monkeypatch, "CHANNEL_ALLOWED_HOSTS", "10.0.0.5")
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])

    refused = await _post_channel(client, headers, "http://10.0.0.6:8080")
    assert refused.status_code == 422
    public = await _post_channel(client, headers, "https://evolution.example.com")
    assert public.status_code == 201


@pytest.mark.parametrize(
    ("name", "value"),
    [("TOOL_ALLOWED_HOSTS", "10.0.0.5"), ("TOOL_SSRF_DISABLED", "true")],
)
async def test_tool_settings_do_not_apply_to_channels(client, monkeypatch, name, value):
    _set_env(monkeypatch, name, value)
    tenant = await _create_tenant(client)
    response = await _post_channel(client, _auth(tenant["api_key"]), PRIVATE_URL)
    assert response.status_code == 422


async def test_tool_allowlist_does_not_restrict_channels(client, monkeypatch):
    """`TOOL_ALLOWED_HOSTS` is authoritative for tools only."""
    _set_env(monkeypatch, "TOOL_ALLOWED_HOSTS", "mock-tools")
    tenant = await _create_tenant(client)
    response = await _post_channel(
        client, _auth(tenant["api_key"]), "https://evolution.example.com"
    )
    assert response.status_code == 201


async def test_webchat_config_is_not_checked(client):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    agent_id = await _create_agent(client, headers)
    response = await client.post(
        "/channels",
        json={
            "type": "webchat",
            "name": "Web",
            "agent_id": agent_id,
            "config": {"base_url": PRIVATE_URL},
        },
        headers=headers,
    )
    assert response.status_code == 201


# --- outbound calls --------------------------------------------------------


def _contact(channel: Channel) -> Contact:
    return Contact(
        id=uuid.uuid4(),
        tenant_id=channel.tenant_id,
        channel_id=channel.id,
        external_user_id="5511999998888",
    )


def _incoming(channel: Channel) -> IncomingMessage:
    from datetime import UTC, datetime

    return IncomingMessage(
        tenant_id=channel.tenant_id,
        channel_id=channel.id,
        external_user_id="5511999998888",
        external_message_id="3EB0C767D26A1D5F9E1B",
        type="image",
        content="",
        timestamp=datetime.now(UTC),
        raw={"_media": {"kind": "image", "mimetype": "image/jpeg"}},
    )


def _calls(adapter: EvolutionAdapter, channel: Channel):
    contact = _contact(channel)
    return {
        "fetch_media": lambda: adapter.fetch_media(_incoming(channel), channel),
        "send_message": lambda: adapter.send_message(
            OutgoingMessage(conversation_id=uuid.uuid4(), content="oi"), contact, channel
        ),
        "mark_read": lambda: adapter.mark_read("wamid.1", contact, channel),
        "send_typing": lambda: adapter.send_typing(contact, channel, 1.0),
    }


METHODS = ["fetch_media", "send_message", "mark_read", "send_typing"]


@pytest.mark.parametrize("method", METHODS)
async def test_stored_private_base_url_is_refused_on_every_call(
    client, db_session, method
):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    channel = await _force_base_url(db_session, channel_id, PRIVATE_URL)

    with respx.mock(assert_all_called=False) as router:
        route = router.route().mock(return_value=httpx.Response(200, json={}))
        with capture_logs() as logs, pytest.raises(SSRFError):
            await _calls(EvolutionAdapter(), channel)[method]()
        assert route.call_count == 0

    refused = [log for log in logs if log["event"] == "channel.endpoint_refused"]
    assert len(refused) == 1
    assert refused[0]["log_level"] == "warning"
    assert refused[0]["channel_id"] == channel_id
    assert refused[0]["tenant_id"] == str(channel.tenant_id)
    assert "10.0.0.5" in refused[0]["reason"]
    assert APIKEY not in repr(refused[0])


@pytest.mark.parametrize("method", METHODS)
async def test_hostname_resolving_to_private_address_is_refused_on_call(
    client, db_session, monkeypatch, method
):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    channel = await _force_base_url(db_session, channel_id, "https://evo.rebind.test")

    def getaddrinfo(host, port, *args, **kwargs):
        assert host == "evo.rebind.test"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    with respx.mock(assert_all_called=False) as router:
        route = router.route().mock(return_value=httpx.Response(200, json={}))
        with pytest.raises(SSRFError, match="blocked internal address"):
            await _calls(EvolutionAdapter(), channel)[method]()
        assert route.call_count == 0


@pytest.mark.parametrize("method", ["send_message", "mark_read", "send_typing"])
async def test_listed_host_is_called(client, db_session, monkeypatch, method):
    _set_env(monkeypatch, "CHANNEL_ALLOWED_HOSTS", "10.0.0.5")
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    channel = await _force_base_url(db_session, channel_id, PRIVATE_URL)

    with respx.mock(assert_all_called=False) as router:
        route = router.post(url__startswith=f"{PRIVATE_URL}/").mock(
            return_value=httpx.Response(200, json={})
        )
        await _calls(EvolutionAdapter(), channel)[method]()
        assert route.call_count == 1


async def test_redirects_are_not_followed(client, db_session):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    channel = await _force_base_url(db_session, channel_id, "https://evolution.example.com")

    with respx.mock(assert_all_called=False) as router:
        router.post(url__startswith="https://evolution.example.com/").mock(
            return_value=httpx.Response(
                302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
            )
        )
        metadata = router.route(host="169.254.169.254").mock(
            return_value=httpx.Response(200, json={})
        )
        with pytest.raises(httpx.HTTPStatusError):
            await _calls(EvolutionAdapter(), channel)["send_message"]()
        assert metadata.call_count == 0


# --- callers fail safely ---------------------------------------------------


async def test_webhook_media_on_refused_channel_still_returns_200(client, db_session):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    await _force_base_url(db_session, channel_id, PRIVATE_URL)

    import json

    payload = json.loads((FIXTURES / "evolution_image_upsert.json").read_text())
    with respx.mock(assert_all_called=False) as router:
        route = router.route().mock(return_value=httpx.Response(200, json={}))
        response = await client.post(
            f"/webhooks/whatsapp/{channel_id}", json=payload, headers={"apikey": APIKEY}
        )
        assert route.call_count == 0
    assert response.status_code == 200

    result = await db_session.execute(
        select(Message).where(Message.channel_id == uuid.UUID(channel_id))
    )
    (message,) = result.scalars().all()
    assert message.type == "unsupported"


async def test_operator_reply_on_refused_channel_returns_502(client, db_session):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    import json

    payload = json.loads((FIXTURES / "evolution_messages_upsert.json").read_text())
    inbound = await client.post(
        f"/webhooks/whatsapp/{channel_id}", json=payload, headers={"apikey": APIKEY}
    )
    assert inbound.status_code == 200
    conversation_id = (await client.get("/conversations", headers=headers)).json()[0]["id"]
    await _force_base_url(db_session, channel_id, PRIVATE_URL)

    with respx.mock(assert_all_called=False) as router:
        route = router.route().mock(return_value=httpx.Response(200, json={}))
        response = await client.post(
            f"/conversations/{conversation_id}/messages",
            json={"content": "Olá, aqui é o atendente."},
            headers=headers,
        )
        assert route.call_count == 0

    assert response.status_code == 502
    assert response.json()["error"]["message"] == "channel endpoint refused"
    messages = await client.get(f"/conversations/{conversation_id}/messages", headers=headers)
    assert [m["role"] for m in messages.json()] == ["user"]


# --- the two entry points --------------------------------------------------


def test_validate_channel_base_url_does_no_dns(monkeypatch):
    def getaddrinfo(*args, **kwargs):
        raise AssertionError("validate_channel_base_url must not resolve DNS")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    validate_channel_base_url("https://evo.anything.test")
    with pytest.raises(SSRFError):
        validate_channel_base_url(PRIVATE_URL)


def test_listed_host_still_needs_http_scheme(monkeypatch):
    _set_env(monkeypatch, "CHANNEL_ALLOWED_HOSTS", "10.0.0.5")
    with pytest.raises(SSRFError, match="scheme"):
        validate_channel_base_url("file://10.0.0.5/etc/passwd")


async def test_assert_safe_channel_url_resolves_names():
    await assert_safe_channel_url("https://evolution.example.com")
    with pytest.raises(SSRFError):
        await assert_safe_channel_url(PRIVATE_URL)


# --- trailing-dot hosts ----------------------------------------------------
#
# `host.` is the same name as `host` to a resolver, so the static check must
# judge it the same way: a trailing dot must neither dodge the internal-name
# and address rules nor stop an operator-listed host from matching.

TRAILING_DOT_URLS = [
    "http://10.0.0.5.:8080",
    "http://169.254.169.254.",
    "http://localhost.",
    "http://metadata.google.internal.",
]


@pytest.mark.parametrize("url", TRAILING_DOT_URLS)
async def test_create_refuses_trailing_dot_internal_hosts(client, db_session, url):
    tenant = await _create_tenant(client)
    response = await _post_channel(client, _auth(tenant["api_key"]), url)

    assert response.status_code == 422
    assert response.json()["error"]["message"].startswith("invalid base_url: ")
    assert await _channel_rows(db_session, tenant["id"]) == []


@pytest.mark.parametrize("url", TRAILING_DOT_URLS)
async def test_update_refuses_trailing_dot_internal_hosts(client, db_session, url):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    response = await client.patch(
        f"/channels/{channel_id}",
        json={"config": {"base_url": url, "instance": "harness-instance"}},
        headers=headers,
    )

    assert response.status_code == 422
    assert response.json()["error"]["message"].startswith("invalid base_url: ")
    db_session.expire_all()
    (row,) = await _channel_rows(db_session, tenant["id"])
    assert row.config["base_url"] == "https://evolution.example.com"


@pytest.mark.parametrize("url", ["http://evolution:8080", "http://evolution.:8080"])
async def test_listed_host_matches_with_trailing_dot(client, monkeypatch, url):
    _set_env(monkeypatch, "CHANNEL_ALLOWED_HOSTS", "evolution")
    tenant = await _create_tenant(client)
    response = await _post_channel(client, _auth(tenant["api_key"]), url)
    assert response.status_code == 201

    def getaddrinfo(*args, **kwargs):
        raise AssertionError("a listed host must not be resolved")

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    validate_channel_base_url(url)
    await assert_safe_channel_url(url)


def test_trailing_dot_only_host_is_refused():
    with pytest.raises(SSRFError, match="no host"):
        validate_channel_base_url("http://.:8080")


# Tool SSRF behaviour is out of scope (PRODUCT.md): these pin what the tool
# entry points did before the channel trailing-dot fix, so it cannot leak in.


@pytest.mark.parametrize("url", TRAILING_DOT_URLS)
def test_tool_static_check_unchanged_for_trailing_dot(url):
    validate_tool_endpoint(url)  # accepted statically, as before


def test_tool_allowlist_unchanged_for_trailing_dot(monkeypatch):
    _set_env(monkeypatch, "TOOL_ALLOWED_HOSTS", "evolution")
    validate_tool_endpoint("http://evolution:8080")
    with pytest.raises(SSRFError, match="not in TOOL_ALLOWED_HOSTS: evolution."):
        validate_tool_endpoint("http://evolution.:8080")


async def test_assert_safe_url_unchanged_for_trailing_dot(monkeypatch):
    """The tool per-call check still resolves the dotted name as given."""
    looked_up: list[str] = []

    def getaddrinfo(host, port, *args, **kwargs):
        looked_up.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    with pytest.raises(SSRFError, match="blocked internal address for host 10.0.0.5."):
        await assert_safe_url("http://10.0.0.5.:8080")
    assert looked_up == ["10.0.0.5."]


# --- refused channel endpoint stops the turn early -------------------------


async def _evolution_conversation(client, db_session) -> tuple[Channel, uuid.UUID, list[Message]]:
    """An Evolution conversation with one inbound text, its channel then
    forced to a private `base_url` (humanized, so a healthy turn would send a
    read receipt first)."""
    import json

    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)
    payload = json.loads((FIXTURES / "evolution_messages_upsert.json").read_text())
    inbound = await client.post(
        f"/webhooks/whatsapp/{channel_id}", json=payload, headers={"apikey": APIKEY}
    )
    assert inbound.status_code == 200

    channel = await db_session.get(Channel, uuid.UUID(channel_id))
    channel.config = {**channel.config, "base_url": PRIVATE_URL, "humanize": True}
    await db_session.commit()

    result = await db_session.execute(
        select(Message).where(Message.channel_id == channel.id).order_by(Message.created_at)
    )
    messages = list(result.scalars().all())
    assert len(messages) == 1
    return channel, messages[0].conversation_id, messages


async def _runs(db_session, conversation_id: uuid.UUID) -> list[AgentRun]:
    result = await db_session.execute(
        select(AgentRun).where(AgentRun.conversation_id == conversation_id)
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


async def test_refused_channel_endpoint_stops_turn_before_model(client, db_session):
    channel, conversation_id, messages = await _evolution_conversation(client, db_session)
    channel_id = str(channel.id)
    provider = FakeLLMProvider([LLMResponse(content="Seu pedido chega amanhã.")])

    with respx.mock(assert_all_called=False) as router:
        route = router.route().mock(return_value=httpx.Response(200, json={}))
        with capture_logs() as logs:
            result = await process_turn(
                db_session, conversation_id, messages, AgentRuntime(provider=provider)
            )
        assert route.call_count == 0

    assert result is None
    assert provider.call_count == 0
    await db_session.rollback()
    assert await _runs(db_session, conversation_id) == []
    assert await _outbound(db_session, conversation_id) == []

    refused = [log for log in logs if log["event"] == "turn.refused_channel_endpoint"]
    assert len(refused) == 1
    assert refused[0]["log_level"] == "warning"
    assert refused[0]["conversation_id"] == str(conversation_id)
    assert refused[0]["channel_id"] == channel_id


async def test_refused_channel_endpoint_does_not_block_tick(client, db_session, fake_redis):
    import os
    import time
    from contextlib import asynccontextmanager

    from app.conversations import debounce
    from app.queue.redis_queue import JOBS_KEY, RedisQueue
    from app.workers.main import run_once
    from app.workers.scheduler import tick

    _, refused_id, _ = await _evolution_conversation(client, db_session)

    # A second, healthy tenant on webchat.
    tenant = await client.post(
        "/tenants", json={"name": "Healthy"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = _auth(tenant.json()["api_key"])
    agent_id = await _create_agent(client, headers)
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent_id},
        headers=headers,
    )
    sent = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel.json()["id"], "user_id": "user-1", "message": "oi"},
        headers=headers,
    )
    assert sent.status_code == 201
    healthy_id = uuid.UUID(sent.json()["conversation_id"])

    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=0.2) is not None

    # The refused conversation comes first in the tick.
    now = time.time()
    await debounce.schedule(fake_redis, refused_id, now)
    await debounce.schedule(fake_redis, healthy_id, now + 1)

    @asynccontextmanager
    async def factory():
        yield db_session

    provider = FakeLLMProvider([LLMResponse(content="Olá! Como posso ajudar?")], repeat_last=True)
    with respx.mock(assert_all_called=False) as router:
        route = router.route().mock(return_value=httpx.Response(200, json={}))
        processed = await tick(
            fake_redis, factory, AgentRuntime(provider=provider), now=now + 5
        )
        assert route.call_count == 0

    assert healthy_id in processed
    assert provider.call_count == 1
    replies = await _outbound(db_session, healthy_id)
    assert [reply.content for reply in replies] == ["Olá! Como posso ajudar?"]
    assert await _outbound(db_session, refused_id) == []
    assert await _runs(db_session, refused_id) == []
