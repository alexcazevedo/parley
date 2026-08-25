"""Evolution API (WhatsApp) adapter: authenticated webhook ingestion and
outbound `sendText`.
"""

import copy
import json
import os
import uuid
from pathlib import Path

import httpx
import pytest
import respx

from app.channels.base import OutgoingMessage
from app.channels.evolution import EvolutionAdapter
from app.channels.webchat import WebChatAdapter
from app.models.channel import Channel, ChannelType
from app.models.contact import Contact
from app.queue.redis_queue import JOBS_KEY
from app.security import secrets as secrets_crypto

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "evolution_messages_upsert.json"


def _fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text())


def _admin_key() -> str:
    return os.environ["ADMIN_API_KEY"]


def _auth(api_key: str) -> dict:
    return {"X-API-Key": api_key}


async def _create_tenant(client, name: str = "Acme") -> dict:
    response = await client.post(
        "/tenants", json={"name": name}, headers={"X-Admin-Key": _admin_key()}
    )
    assert response.status_code == 201
    return response.json()


async def _create_evolution_channel(client, headers: dict, apikey: str = "evo-secret-xyz") -> str:
    agent = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Hi.", "model": "gpt-4o-mini"},
        headers=headers,
    )
    channel = await client.post(
        "/channels",
        json={
            "type": "whatsapp_evolution",
            "name": "WhatsApp",
            "agent_id": agent.json()["id"],
            "config": {"base_url": "https://evolution.example.com", "instance": "harness-instance"},
            "secret": apikey,
        },
        headers=headers,
    )
    assert channel.status_code == 201
    return channel.json()["id"]


async def test_valid_webhook_persists_message_and_enqueues_job(client, fake_redis):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_fixture(),
        headers={"apikey": "evo-secret-xyz"},
    )
    assert response.status_code == 200

    conversations = await client.get("/conversations", headers=headers)
    assert len(conversations.json()) == 1
    conversation_id = conversations.json()[0]["id"]

    messages = await client.get(f"/conversations/{conversation_id}/messages", headers=headers)
    assert len(messages.json()) == 1
    assert messages.json()[0]["content"] == "Oi, queria saber onde está meu pedido"

    assert await fake_redis.llen(JOBS_KEY) == 1


async def test_wrong_apikey_returns_401(client, fake_redis):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_fixture(),
        headers={"apikey": "wrong-key"},
    )
    assert response.status_code == 401
    assert await fake_redis.llen(JOBS_KEY) == 0


async def test_missing_apikey_header_returns_401(client):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    response = await client.post(f"/webhooks/whatsapp/{channel_id}", json=_fixture())
    assert response.status_code == 401


async def test_from_me_is_ignored_with_200_and_no_row(client, fake_redis):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    payload = copy.deepcopy(_fixture())
    payload["data"]["key"]["fromMe"] = True

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=payload,
        headers={"apikey": "evo-secret-xyz"},
    )
    assert response.status_code == 200

    conversations = await client.get("/conversations", headers=headers)
    assert conversations.json() == []
    assert await fake_redis.llen(JOBS_KEY) == 0


async def test_non_text_message_is_ignored_with_200(client, fake_redis):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    payload = copy.deepcopy(_fixture())
    payload["data"]["message"] = {"imageMessage": {"caption": "look"}}

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=payload,
        headers={"apikey": "evo-secret-xyz"},
    )
    assert response.status_code == 200
    assert await fake_redis.llen(JOBS_KEY) == 0


async def test_unhandled_event_is_ignored_with_200(client, fake_redis):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    payload = copy.deepcopy(_fixture())
    payload["event"] = "connection.update"

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=payload,
        headers={"apikey": "evo-secret-xyz"},
    )
    assert response.status_code == 200
    assert await fake_redis.llen(JOBS_KEY) == 0


async def test_duplicate_key_id_inserts_once(client, fake_redis):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_evolution_channel(client, headers)

    payload = _fixture()
    first = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=payload,
        headers={"apikey": "evo-secret-xyz"},
    )
    second = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=payload,
        headers={"apikey": "evo-secret-xyz"},
    )
    assert first.status_code == 200
    assert second.status_code == 200

    conversations = await client.get("/conversations", headers=headers)
    conversation_id = conversations.json()[0]["id"]
    messages = await client.get(f"/conversations/{conversation_id}/messages", headers=headers)
    assert len(messages.json()) == 1

    assert await fake_redis.llen(JOBS_KEY) == 1


async def test_unknown_channel_returns_404(client):
    response = await client.post(
        f"/webhooks/whatsapp/{uuid.uuid4()}",
        json=_fixture(),
        headers={"apikey": "whatever"},
    )
    assert response.status_code == 404


@pytest.mark.asyncio
@respx.mock
async def test_send_message_posts_to_evolution_send_text():
    channel = Channel(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type=ChannelType.WHATSAPP_EVOLUTION,
        name="WhatsApp",
        agent_id=uuid.uuid4(),
        config={"base_url": "https://evolution.example.com", "instance": "harness-instance"},
        secret_encrypted=secrets_crypto.encrypt("evo-secret-xyz"),
    )
    contact = Contact(
        id=uuid.uuid4(),
        tenant_id=channel.tenant_id,
        channel_id=channel.id,
        external_user_id="5511999998888",
    )
    route = respx.post(
        "https://evolution.example.com/message/sendText/harness-instance"
    ).mock(return_value=httpx.Response(200, json={"key": {"id": "abc"}}))

    adapter = EvolutionAdapter()
    message = OutgoingMessage(conversation_id=uuid.uuid4(), content="Seu pedido chegou!")
    await adapter.send_message(message, contact, channel)

    assert route.called
    request = route.calls.last.request
    assert request.headers["apikey"] == "evo-secret-xyz"
    assert json.loads(request.content) == {
        "number": "5511999998888",
        "text": "Seu pedido chegou!",
    }


@pytest.mark.asyncio
@respx.mock
async def test_send_message_raises_on_non_2xx():
    channel = Channel(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type=ChannelType.WHATSAPP_EVOLUTION,
        name="WhatsApp",
        agent_id=uuid.uuid4(),
        config={"base_url": "https://evolution.example.com", "instance": "harness-instance"},
        secret_encrypted=secrets_crypto.encrypt("evo-secret-xyz"),
    )
    contact = Contact(
        id=uuid.uuid4(),
        tenant_id=channel.tenant_id,
        channel_id=channel.id,
        external_user_id="5511999998888",
    )
    respx.post("https://evolution.example.com/message/sendText/harness-instance").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )

    adapter = EvolutionAdapter()
    message = OutgoingMessage(conversation_id=uuid.uuid4(), content="hi")
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.send_message(message, contact, channel)


@pytest.mark.asyncio
@respx.mock
async def test_mark_read_posts_to_evolution_mark_message_as_read():
    channel = Channel(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type=ChannelType.WHATSAPP_EVOLUTION,
        name="WhatsApp",
        agent_id=uuid.uuid4(),
        config={"base_url": "https://evolution.example.com", "instance": "harness-instance"},
        secret_encrypted=secrets_crypto.encrypt("evo-secret-xyz"),
    )
    contact = Contact(
        id=uuid.uuid4(),
        tenant_id=channel.tenant_id,
        channel_id=channel.id,
        external_user_id="5511999998888",
    )
    route = respx.post(
        "https://evolution.example.com/chat/markMessageAsRead/harness-instance"
    ).mock(return_value=httpx.Response(200, json={}))

    adapter = EvolutionAdapter()
    await adapter.mark_read("wamid.abc123", contact, channel)

    assert route.called
    request = route.calls.last.request
    assert request.headers["apikey"] == "evo-secret-xyz"
    assert json.loads(request.content) == {
        "readMessages": [
            {
                "remoteJid": "5511999998888@s.whatsapp.net",
                "fromMe": False,
                "id": "wamid.abc123",
            }
        ]
    }


@pytest.mark.asyncio
@respx.mock
async def test_mark_read_raises_on_non_2xx():
    channel = Channel(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type=ChannelType.WHATSAPP_EVOLUTION,
        name="WhatsApp",
        agent_id=uuid.uuid4(),
        config={"base_url": "https://evolution.example.com", "instance": "harness-instance"},
        secret_encrypted=secrets_crypto.encrypt("evo-secret-xyz"),
    )
    contact = Contact(
        id=uuid.uuid4(),
        tenant_id=channel.tenant_id,
        channel_id=channel.id,
        external_user_id="5511999998888",
    )
    respx.post("https://evolution.example.com/chat/markMessageAsRead/harness-instance").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )

    adapter = EvolutionAdapter()
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.mark_read("wamid.abc123", contact, channel)


@pytest.mark.asyncio
@respx.mock
async def test_send_typing_posts_to_evolution_send_presence():
    channel = Channel(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type=ChannelType.WHATSAPP_EVOLUTION,
        name="WhatsApp",
        agent_id=uuid.uuid4(),
        config={"base_url": "https://evolution.example.com", "instance": "harness-instance"},
        secret_encrypted=secrets_crypto.encrypt("evo-secret-xyz"),
    )
    contact = Contact(
        id=uuid.uuid4(),
        tenant_id=channel.tenant_id,
        channel_id=channel.id,
        external_user_id="5511999998888",
    )
    route = respx.post(
        "https://evolution.example.com/chat/sendPresence/harness-instance"
    ).mock(return_value=httpx.Response(200, json={}))

    adapter = EvolutionAdapter()
    await adapter.send_typing(contact, channel, 1.5)

    assert route.called
    request = route.calls.last.request
    assert request.headers["apikey"] == "evo-secret-xyz"
    assert json.loads(request.content) == {
        "number": "5511999998888",
        "presence": "composing",
        "delay": 1500,
    }


@pytest.mark.asyncio
@respx.mock
async def test_send_typing_raises_on_non_2xx():
    channel = Channel(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type=ChannelType.WHATSAPP_EVOLUTION,
        name="WhatsApp",
        agent_id=uuid.uuid4(),
        config={"base_url": "https://evolution.example.com", "instance": "harness-instance"},
        secret_encrypted=secrets_crypto.encrypt("evo-secret-xyz"),
    )
    contact = Contact(
        id=uuid.uuid4(),
        tenant_id=channel.tenant_id,
        channel_id=channel.id,
        external_user_id="5511999998888",
    )
    respx.post("https://evolution.example.com/chat/sendPresence/harness-instance").mock(
        return_value=httpx.Response(500, json={"error": "boom"})
    )

    adapter = EvolutionAdapter()
    with pytest.raises(httpx.HTTPStatusError):
        await adapter.send_typing(contact, channel, 1.5)


@pytest.mark.asyncio
async def test_webchat_adapter_hooks_are_noop():
    channel = Channel(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        type=ChannelType.WEBCHAT,
        name="Web Chat",
        agent_id=uuid.uuid4(),
        config={},
    )
    contact = Contact(
        id=uuid.uuid4(),
        tenant_id=channel.tenant_id,
        channel_id=channel.id,
        external_user_id="web-user-1",
    )

    adapter = WebChatAdapter()
    assert await adapter.mark_read("msg-1", contact, channel) is None
    assert await adapter.send_typing(contact, channel, 1.0) is None
