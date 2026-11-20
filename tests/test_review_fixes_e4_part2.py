"""Review fixes of the conversation-correctness run, part 2 (ticket 09).

1. Pending media of a batch is downloaded whatever the conversation's state and
   whether or not an agent is available; a refused endpoint marks it failed.
2. `fetch_pending_media` commits its result: a rollback later in the turn does
   not download again, a failed download stays failed; no download once the
   turn has lost its lock.
3. Every handoff without configured text sends `DEFAULT_HANDOFF_MESSAGE`.
4. `strip_base64` handles payloads nested far deeper than the recursion limit.
5. A rate-limited media message is stored as "media not received".
6. The network guard refuses `sendto` outside the box, answers `test` locally
   and allows the configured database and Redis hosts.
"""

# ruff: noqa: F811  (the imported `adapter` / `media_root` fixtures are test arguments)

import _socket
import socket
import uuid

import httpx
import pytest
import respx
from sqlalchemy import update

import app.api.webhooks as webhooks_module
import app.models.agent as agent_module
from app.agents.runtime import AgentRuntime
from app.channels.evolution import strip_base64
from app.config import get_settings
from app.conversations import turn as turn_module
from app.conversations.turn import LockLost, TurnFailed, process_turn
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.conversation import Conversation, ConversationState
from app.models.message import Message
from app.queue.redis_queue import JOBS_KEY
from tests.test_evolution import _auth, _create_evolution_channel, _create_tenant
from tests.test_handoff_default_message import _clear_handoff_message
from tests.test_handoff_reply_owed import (  # noqa: F401  (adapter is a fixture)
    _handoff_runtime,
    _inbound,
    _setup,
    _stored_replies,
    adapter,
)
from tests.test_webhook_fast import (
    APIKEY,
    GET_BASE64_URL,
    SEND_TEXT_URL,
    _drain_jobs,
    _fixture,
    _media_response,
    _png,
    _rows,
    _run_turn,
    media_root,  # noqa: F401  (fixture)
)


def _stored_files(media_root) -> list:
    return [p for p in media_root.rglob("*") if p.is_file()]


async def _image_webhook(client, db_session) -> tuple[dict, str, Message]:
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_fixture("evolution_image_upsert.json"),
        headers=APIKEY,
    )
    assert response.json() == {"status": "ok"}
    (row,) = await _rows(db_session, channel_id)
    return tenant, channel_id, row


async def _set_state(db_session, conversation_id, state) -> None:
    await db_session.execute(
        update(Conversation).where(Conversation.id == conversation_id).values(state=state)
    )
    await db_session.commit()


# --- 1. media is downloaded whatever the conversation's state ----------------------


@respx.mock
async def test_photo_during_handoff_is_downloaded_and_served_without_ai_reply(
    client, fake_redis, db_session, media_root
):
    route = respx.post(GET_BASE64_URL).mock(return_value=_media_response(_png(), "image/png"))
    send = respx.post(SEND_TEXT_URL).mock(return_value=httpx.Response(200, json={}))
    tenant, channel_id, row = await _image_webhook(client, db_session)
    await _set_state(db_session, row.conversation_id, ConversationState.HUMAN_HANDOFF)

    await _drain_jobs(fake_redis)
    provider = FakeLLMProvider([])
    await _run_turn(fake_redis, db_session, AgentRuntime(provider=provider))

    assert route.call_count == 1
    assert send.call_count == 0
    assert provider.call_count == 0
    (row,) = await _rows(db_session, channel_id)
    assert row.type == "image"
    assert row.media_key.startswith(f"{tenant['id']}/")
    media = await client.get(f"/media/{row.id}", headers=_auth(tenant["api_key"]))
    assert media.status_code == 200
    assert media.content == _png()


@respx.mock
async def test_photo_is_downloaded_when_no_agent_is_available(
    client, fake_redis, db_session, media_root, monkeypatch
):
    route = respx.post(GET_BASE64_URL).mock(return_value=_media_response(_png(), "image/png"))
    send = respx.post(SEND_TEXT_URL).mock(return_value=httpx.Response(200, json={}))
    _, channel_id, _ = await _image_webhook(client, db_session)

    async def no_agent(session, conversation):
        return None

    monkeypatch.setattr(turn_module, "load_conversation_agent", no_agent)
    await _drain_jobs(fake_redis)
    await _run_turn(fake_redis, db_session, AgentRuntime(provider=FakeLLMProvider([])))

    assert route.call_count == 1
    assert send.call_count == 0
    (row,) = await _rows(db_session, channel_id)
    assert row.type == "image" and row.media_key


async def test_refused_endpoint_marks_pending_media_as_failed_download(
    client, fake_redis, db_session, media_root
):
    with respx.mock(assert_all_called=False) as router:
        route = router.route().mock(return_value=_media_response(_png(), "image/png"))
        _, channel_id, _ = await _image_webhook(client, db_session)
        channel = await db_session.get(Channel, uuid.UUID(channel_id))
        channel.config = {"base_url": "http://10.0.0.5:8080", "instance": "harness-instance"}
        await db_session.commit()

        await _drain_jobs(fake_redis)
        await _run_turn(fake_redis, db_session, AgentRuntime(provider=FakeLLMProvider([])))

    assert route.call_count == 0
    (row,) = await _rows(db_session, channel_id)
    assert row.type == "unsupported"
    assert row.content == "[mídia não recebida]"
    assert row.media_key is None


# --- 2. the download's result is committed -------------------------------------------


class FailingRuntime:
    """Text-in/text-out runtime that raises: the turn rolls back."""

    async def run(self, conversation, input_text: str) -> str:
        raise RuntimeError("model unavailable")


class PlainRuntime:
    async def run(self, conversation, input_text: str) -> str:
        return "Recebi."


@respx.mock
async def test_turn_rolled_back_after_download_does_not_download_again(
    client, fake_redis, db_session, media_root
):
    route = respx.post(GET_BASE64_URL).mock(return_value=_media_response(_png(), "image/png"))
    respx.post(SEND_TEXT_URL).mock(return_value=httpx.Response(200, json={}))
    _, channel_id, row = await _image_webhook(client, db_session)
    conversation_id = row.conversation_id

    with pytest.raises(TurnFailed):
        await process_turn(db_session, conversation_id, [row], FailingRuntime())
    (row,) = await _rows(db_session, channel_id)
    assert row.media_key

    await process_turn(db_session, conversation_id, [row], PlainRuntime())

    assert route.call_count == 1
    assert len(_stored_files(media_root)) == 1


@respx.mock
async def test_failed_download_stays_failed_after_a_rollback(
    client, fake_redis, db_session, media_root
):
    route = respx.post(GET_BASE64_URL).mock(return_value=httpx.Response(500, json={}))
    respx.post(SEND_TEXT_URL).mock(return_value=httpx.Response(200, json={}))
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    for name in ("evolution_messages_upsert.json", "evolution_image_upsert.json"):
        await client.post(f"/webhooks/whatsapp/{channel_id}", json=_fixture(name), headers=APIKEY)
    batch = await _rows(db_session, channel_id)
    assert [m.type for m in batch] == ["text", "image"]
    conversation_id = batch[0].conversation_id

    with pytest.raises(TurnFailed):
        await process_turn(db_session, conversation_id, batch, FailingRuntime())
    batch = await _rows(db_session, channel_id)
    assert batch[1].type == "unsupported"

    await process_turn(db_session, conversation_id, batch, PlainRuntime())

    assert route.call_count == 1
    assert (await _rows(db_session, channel_id))[1].type == "unsupported"


@respx.mock
async def test_no_download_once_the_lock_is_lost(client, fake_redis, db_session, media_root):
    route = respx.post(GET_BASE64_URL).mock(return_value=_media_response(_png(), "image/png"))
    send = respx.post(SEND_TEXT_URL).mock(return_value=httpx.Response(200, json={}))
    _, channel_id, row = await _image_webhook(client, db_session)

    async def not_ours() -> bool:
        return False

    result = await process_turn(
        db_session, row.conversation_id, [row], PlainRuntime(), ownership_check=not_ours
    )

    assert isinstance(result, LockLost)
    assert route.call_count == 0
    assert send.call_count == 0
    (row,) = await _rows(db_session, channel_id)
    assert row.type == "image" and row.media_key is None


# --- 3. default handoff text ---------------------------------------------------------


async def test_tool_handoff_without_configured_text_sends_the_default(
    client, fake_redis, db_session, test_engine, adapter
):
    _, _, conversation_id = await _setup(client, fake_redis, db_session)
    await _clear_handoff_message(db_session, conversation_id)
    messages = await _inbound(db_session, conversation_id)

    result = await process_turn(db_session, conversation_id, messages, _handoff_runtime())

    assert isinstance(result, Message)
    default = agent_module.DEFAULT_HANDOFF_MESSAGE
    assert adapter.sent == [default]
    assert await _stored_replies(test_engine, conversation_id) == [default]


def test_default_handoff_message_is_the_agent_default():
    default = agent_module.DEFAULT_HANDOFF_MESSAGE
    assert default == "Vou te transferir para um atendente humano."
    assert Agent.__table__.c.handoff_message.default.arg == default


# --- 4. strip_base64 without recursion ---------------------------------------------


def test_strip_base64_handles_a_payload_nested_5000_levels_deep():
    payload: dict = {"base64": "x", "keep": 0}
    node = payload
    for depth in range(1, 5000):
        child: dict = {"base64": "x", "keep": depth}
        node["next"] = [child] if depth % 2 else child
        node = child

    stripped = strip_base64(payload)

    node, depth = stripped, 0
    while True:
        assert "base64" not in node
        assert node["keep"] == depth
        if "next" not in node:
            break
        nxt = node["next"]
        node = nxt[0] if isinstance(nxt, list) else nxt
        depth += 1
    assert depth == 4999
    assert payload["base64"] == "x"  # the input is untouched


def test_strip_base64_copies_and_keeps_shared_and_scalar_values():
    shared = {"base64": "s", "v": [1, 2]}
    payload = {"a": shared, "b": shared, "c": [shared, "t", None, 3.5], "jpegThumbnail": "j"}

    stripped = strip_base64(payload)

    assert stripped == {
        "a": {"v": [1, 2]},
        "b": {"v": [1, 2]},
        "c": [{"v": [1, 2]}, "t", None, 3.5],
    }
    assert shared == {"base64": "s", "v": [1, 2]}
    stripped["a"]["v"].append(3)
    assert shared["v"] == [1, 2]


# --- 5. rate-limited media -----------------------------------------------------------


@respx.mock
async def test_rate_limited_media_is_stored_as_media_not_received(
    client, fake_redis, db_session, media_root, monkeypatch
):
    route = respx.post(GET_BASE64_URL).mock(return_value=_media_response(_png(), "image/png"))
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))

    async def over_limit(*args, **kwargs) -> bool:
        return True

    monkeypatch.setattr(webhooks_module, "inbound_over_limit", over_limit)
    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_fixture("evolution_image_upsert.json"),
        headers=APIKEY,
    )

    assert response.json() == {"status": "rate_limited"}
    assert await fake_redis.llen(JOBS_KEY) == 0
    assert route.call_count == 0
    (row,) = await _rows(db_session, channel_id)
    assert row.type == "unsupported"
    assert row.content == "[mídia não recebida]"
    assert row.media_key is None


# --- 6. network guard ----------------------------------------------------------------


def test_sendto_outside_the_box_is_refused(network_guard):
    # The limited broadcast address: without SO_BROADCAST the kernel refuses it
    # too, so nothing leaves the machine even if the guard let it through.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(OSError, match="network guard"):
            sock.sendto(b"x", ("255.255.255.255", 9))
    finally:
        sock.close()

    assert network_guard.attempts == ["255.255.255.255"]
    network_guard.attempts.clear()


def test_sendto_loopback_is_not_refused(network_guard):
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(("127.0.0.1", 0))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.sendto(b"ping", server.getsockname())
        server.settimeout(1)
        assert server.recv(16) == b"ping"
    finally:
        sock.close()
        server.close()

    network_guard.check()


def _forbid_real_dns(monkeypatch) -> list:
    """`socket.getaddrinfo` reaches the resolver through `_socket.getaddrinfo`."""
    calls: list = []

    def resolver(host, *args, **kwargs):
        calls.append(host)
        raise OSError("real DNS reached in a test")

    monkeypatch.setattr(_socket, "getaddrinfo", resolver)
    return calls


def test_lookup_of_test_is_answered_locally(network_guard, monkeypatch):
    calls = _forbid_real_dns(monkeypatch)

    answers = socket.getaddrinfo("test", 80)

    assert calls == []
    assert {info[4][0] for info in answers} == {"127.0.0.1"}
    assert all(info[4][1] == 80 for info in answers)
    network_guard.check()


def test_hosts_of_configured_database_and_redis_are_allowed(network_guard, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(
        settings, "database_url", "postgresql+asyncpg://u:p@db.internal:5432/harness_test"
    )
    monkeypatch.setattr(settings, "redis_url", "redis://cache.internal:6379/0")
    resolved: list = []

    def resolver(host, port, *args, **kwargs):
        resolved.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.9.8.7", port))]

    monkeypatch.setattr(_socket, "getaddrinfo", resolver)
    for host, port in (("db.internal", 5432), ("cache.internal", 6379)):
        assert socket.getaddrinfo(host, port)[0][4] == ("10.9.8.7", port)

    assert resolved == ["db.internal", "cache.internal"]
    assert "10.9.8.7" in network_guard.allowed_addresses
    network_guard.check()
    with pytest.raises(OSError, match="network guard"):
        socket.getaddrinfo("elsewhere.internal", 80)
    network_guard.attempts.clear()
