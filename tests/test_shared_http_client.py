"""One process-wide HTTP client for model, STT, channel and tool calls."""

import uuid

import httpx
import pytest
import respx

from app import http_client
from app.channels.base import OutgoingMessage
from app.channels.evolution import EvolutionAdapter
from app.config import get_settings
from app.llm.base import LLMMessage
from app.llm.providers.openai_compatible import OpenAICompatibleProvider
from app.media.stt import transcribe
from app.models.channel import Channel, ChannelType
from app.models.contact import Contact
from app.models.tool import Tool
from app.security import secrets as secrets_crypto
from app.tools.http import HttpToolExecutor

LLM_URL = "https://llm.example.com/v1"
EVO_URL = "https://evolution.example.com"


@pytest.fixture
def count_clients(monkeypatch):
    created: list[httpx.AsyncClient] = []
    original = httpx.AsyncClient.__init__

    def counting_init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", counting_init)
    return created


def _channel() -> tuple[Channel, Contact]:
    channel = Channel(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type=ChannelType.WHATSAPP_EVOLUTION,
        name="WhatsApp",
        agent_id=uuid.uuid4(),
        config={"base_url": EVO_URL, "instance": "inst"},
        secret_encrypted=secrets_crypto.encrypt("evo-secret"),
    )
    contact = Contact(
        id=uuid.uuid4(),
        tenant_id=channel.tenant_id,
        channel_id=channel.id,
        external_user_id="5511999998888",
    )
    return channel, contact


@pytest.mark.asyncio
@respx.mock
async def test_all_call_sites_share_one_client_with_their_own_timeouts(count_clients):
    respx.post(f"{LLM_URL}/chat/completions").mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": "hi"}}], "usage": {}}
        )
    )
    respx.post(f"{LLM_URL}/audio/transcriptions").mock(
        return_value=httpx.Response(200, json={"text": "ola"})
    )
    send = respx.post(f"{EVO_URL}/message/sendText/inst").mock(
        return_value=httpx.Response(200, json={})
    )
    tool_route = respx.post("https://example.com/tool").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    channel, contact = _channel()
    settings = get_settings().model_copy(
        update={"llm_base_url": LLM_URL, "stt_base_url": None, "stt_timeout_seconds": 17}
    )

    provider = OpenAICompatibleProvider(base_url=LLM_URL, api_key="k", timeout=33)
    await provider.complete([LLMMessage(role="user", content="x")], model="m")
    await transcribe(b"abc", "audio/ogg", settings=settings)
    await EvolutionAdapter().send_message(
        OutgoingMessage(conversation_id=uuid.uuid4(), content="oi"), contact, channel
    )
    tool = Tool(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="t",
        input_schema={},
        endpoint="https://example.com/tool",
        method="POST",
        timeout_seconds=7,
    )
    await HttpToolExecutor()._call(tool, {"a": 1})

    assert len(count_clients) == 1
    assert count_clients[0] is http_client.shared_client()
    assert count_clients[0].follow_redirects is False
    assert send.calls.last.request.extensions["timeout"]["read"] == 10
    assert tool_route.calls.last.request.extensions["timeout"]["read"] == 7
    stt_call = respx.calls[1].request
    assert stt_call.extensions["timeout"]["read"] == 17
    assert respx.calls[0].request.extensions["timeout"]["read"] == 33


@pytest.mark.asyncio
async def test_client_is_created_once_and_recreated_after_close():
    first = http_client.shared_client()
    assert http_client.shared_client() is first
    await http_client.close_shared_client()
    assert first.is_closed
    await http_client.close_shared_client()  # safe twice
    second = http_client.shared_client()
    assert second is not first
    assert not second.is_closed


@pytest.mark.asyncio
async def test_provider_does_not_close_the_shared_client():
    provider = OpenAICompatibleProvider(base_url=LLM_URL, api_key="k")
    shared = http_client.shared_client()
    await provider.aclose()
    assert not shared.is_closed


@pytest.mark.asyncio
async def test_api_lifespan_closes_the_shared_client(monkeypatch):
    from app.main import create_app

    app = create_app()
    shared = http_client.shared_client()
    async with app.router.lifespan_context(app):
        assert not shared.is_closed
    assert shared.is_closed


@pytest.mark.asyncio
async def test_worker_close_connections_closes_client_then_redis(fake_redis):
    from app.workers.main import _close_connections

    shared = http_client.shared_client()
    closed: list[str] = []
    real_aclose = fake_redis.aclose

    async def redis_aclose(*args, **kwargs):
        closed.append("redis_after_client" if shared.is_closed else "redis_before_client")
        await real_aclose(*args, **kwargs)

    monkeypatch_target = fake_redis
    monkeypatch_target.aclose = redis_aclose
    await _close_connections(fake_redis)
    assert shared.is_closed
    assert closed == ["redis_after_client"]


COOKIE_URL = "https://shared-host.example.com/api"


async def _set_cookie_then_call_again() -> tuple[httpx.AsyncClient, httpx.Request]:
    """Request 1 gets a Set-Cookie (tenant A); request 2 (tenant B) goes to the same host."""
    route = respx.get(COOKIE_URL).mock(
        side_effect=[
            httpx.Response(
                200, headers={"Set-Cookie": "session=tenantA-secret; Path=/"}, json={}
            ),
            httpx.Response(200, json={}),
        ]
    )
    client = http_client.shared_client()
    await client.get(COOKIE_URL, timeout=5)
    await client.get(COOKIE_URL, timeout=5)
    assert route.call_count == 2
    return client, route.calls[1].request


@pytest.mark.asyncio
@respx.mock
async def test_cookie_from_one_response_is_not_sent_on_the_next_request():
    client, second = await _set_cookie_then_call_again()
    assert "cookie" not in second.headers
    assert len(client.cookies.jar) == 0


@pytest.mark.asyncio
@respx.mock
async def test_new_client_after_close_also_keeps_no_cookies():
    await http_client.close_shared_client()
    client, second = await _set_cookie_then_call_again()
    assert "cookie" not in second.headers
    assert len(client.cookies.jar) == 0
    assert client.follow_redirects is False
    assert client.timeout == httpx.Timeout(5.0)  # httpx default: no client-level override


@pytest.mark.asyncio
@respx.mock
async def test_explicit_per_request_cookie_header_still_goes_out():
    route = respx.get(COOKIE_URL).mock(return_value=httpx.Response(200, json={}))
    await http_client.shared_client().get(
        COOKIE_URL, headers={"Cookie": "explicit=1"}, timeout=5
    )
    assert route.calls.last.request.headers["cookie"] == "explicit=1"
