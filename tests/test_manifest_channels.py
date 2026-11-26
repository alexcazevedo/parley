"""Manifest channels and secret references: a web chat and an external channel
set up from the file, credentials by secret name, nothing secret in any
response or log line."""

import copy
import json
import os
import time
import uuid
from contextlib import asynccontextmanager

import httpx
import respx
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.agents.runtime import AgentRuntime
from app.events.delivery import deliver_due
from app.events.signing import verify
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.models.channel import Channel, ChannelType
from app.models.manifest_item import ManifestItem
from app.models.message import Message, MessageRole
from app.models.tool import Tool
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.security import secrets as secrets_crypto
from app.tools.http import HttpToolExecutor
from app.tools.registry import ToolRegistry
from app.workers.main import run_once
from app.workers.scheduler import tick
from tests.test_external_channel import _run_turn

TOKEN = "tok-from-the-secret-store"
TOKEN_2 = "tok-replaced-later"
EVO_KEY = "evo-key-from-the-secret-store"
CREDENTIALS_MESSAGE = 'credentials must be secret references; use {"secret": "<name>"}'
ORDERS_URL = "https://orders.example.com/orders"
EVENTS_URL = "https://inbox-manifest.example.com/events"
SECRETS = (TOKEN, TOKEN_2, EVO_KEY)

MANIFEST = {
    "version": 1,
    "agents": [
        {
            "key": "main",
            "name": "Loja",
            "instructions": "Você é o atendente da Loja.",
            "model": "gpt-4o-mini",
            "tool_names": ["get_order"],
        }
    ],
    "tools": [
        {
            "name": "get_order",
            "input_schema": {
                "type": "object",
                "properties": {"order_id": {"type": "string"}},
                "required": ["order_id"],
            },
            "endpoint": ORDERS_URL,
            "method": "GET",
            "auth_config": {"type": "bearer", "token": {"secret": "orders_token"}},
        }
    ],
    "channels": [
        {"key": "site", "type": "webchat", "name": "Site", "agent": "main"},
        {
            "key": "inbox",
            "type": "external",
            "name": "Inbox",
            "agent": "main",
            "config": {"events_url": EVENTS_URL},
        },
    ],
}


def _manifest() -> dict:
    return copy.deepcopy(MANIFEST)


async def _tenant(client) -> tuple[dict, uuid.UUID]:
    response = await client.post(
        "/tenants", json={"name": "Loja"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    body = response.json()
    return {"X-API-Key": body["api_key"]}, uuid.UUID(body["id"])


async def _put(client, headers, name, value):
    response = await client.put(f"/secrets/{name}", json={"value": value}, headers=headers)
    assert response.status_code == 204, response.text


async def _apply(client, headers, data, *, dry_run=False):
    response = await client.post(
        f"/manifest/apply?dry_run={'true' if dry_run else 'false'}", json=data, headers=headers
    )
    for value in SECRETS:
        assert value not in response.text
    return response


def _actions(body) -> dict[tuple[str, str], tuple[str, list[str]]]:
    return {(c["kind"], c["key"]): (c["action"], c["fields"]) for c in body["changes"]}


async def _count(db_session, model, tenant_id) -> int:
    return await db_session.scalar(
        select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
    )


async def _channel(db_session, tenant_id, key) -> Channel:
    item_id = await db_session.scalar(
        select(ManifestItem.item_id).where(
            ManifestItem.tenant_id == tenant_id,
            ManifestItem.kind == "channel",
            ManifestItem.key == key,
        )
    )
    channel = await db_session.get(Channel, item_id)
    await db_session.refresh(channel)
    return channel


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


async def test_channels_apply_answer_sign_and_call_a_tool_with_the_secret(
    client, fake_redis, db_session, test_engine
):
    headers, tenant_id = await _tenant(client)
    await _put(client, headers, "orders_token", TOKEN)

    dry = await _apply(client, headers, _manifest(), dry_run=True)
    assert dry.status_code == 200, dry.text
    assert dry.json()["generated"] == {}
    assert _actions(dry.json())[("channel", "inbox")] == ("create", [])
    assert await _count(db_session, Channel, tenant_id) == 0

    with structlog.testing.capture_logs() as logs:
        response = await _apply(client, headers, _manifest())
    assert response.status_code == 200, response.text
    body = response.json()
    assert _actions(body)[("channel", "site")] == ("create", [])
    events_secret = body["generated"]["channels"]["inbox"]["events_secret"]
    assert set(body["generated"]["channels"]) == {"inbox"} and len(events_secret) >= 32
    assert events_secret not in json.dumps(logs, default=str)
    assert TOKEN not in json.dumps(logs, default=str)

    # Never again on a later apply.
    again = await _apply(client, headers, _manifest())
    assert again.json()["generated"] == {}
    assert {c["action"] for c in again.json()["changes"]} == {"unchanged"}

    site = await _channel(db_session, tenant_id, "site")
    inbox = await _channel(db_session, tenant_id, "inbox")
    assert secrets_crypto.decrypt(inbox.secret_encrypted) == events_secret
    stored = await db_session.scalar(select(Tool).where(Tool.tenant_id == tenant_id))
    assert TOKEN not in stored.auth_config_encrypted

    # A web chat message is answered, and the tool call carries the secret.
    sent = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": str(site.id), "user_id": "u1", "message": "pedido 12345?"},
        headers=headers,
    )
    assert sent.status_code == 201, sent.text
    conversation_id = uuid.UUID(sent.json()["conversation_id"])
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=0.2) is not None
    provider = FakeLLMProvider(
        [
            LLMResponse(
                tool_calls=[ToolCall(id="c1", name="get_order", arguments={"order_id": "12345"})]
            ),
            LLMResponse(content="Seu pedido 12345 já foi enviado."),
        ]
    )
    runtime = AgentRuntime(provider=provider, registry=ToolRegistry([HttpToolExecutor()]))
    with respx.mock(assert_all_called=True) as mock:
        route = mock.get(ORDERS_URL).respond(json={"status": "shipped"})
        await tick(fake_redis, _session_factory(db_session), runtime, now=time.time() + 5)
    assert route.calls.last.request.headers["Authorization"] == f"Bearer {TOKEN}"
    messages = (
        await db_session.execute(
            select(Message).where(Message.conversation_id == conversation_id).order_by(Message.seq)
        )
    ).scalars().all()
    assert messages[-1].role == MessageRole.ASSISTANT
    assert messages[-1].content == "Seu pedido 12345 já foi enviado."

    # The external channel's pushes are signed with the generated secret.
    posted = await client.post(
        "/channels/external/messages",
        json={"channel_id": str(inbox.id), "user_id": "c1", "message": "oi"},
        headers=headers,
    )
    assert posted.status_code == 201, posted.text
    await _run_turn(db_session, uuid.UUID(posted.json()["conversation_id"]))
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200)

    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await deliver_due(factory, http) >= 1
    [push] = [r for r in requests if str(r.url) == EVENTS_URL]
    assert verify(
        events_secret,
        push.headers["X-Parley-Timestamp"],
        push.content,
        push.headers["X-Parley-Signature"],
    )


async def test_a_missing_secret_is_a_422_naming_it_and_nothing_is_written(client, db_session):
    headers, tenant_id = await _tenant(client)
    data = _manifest()
    data["channels"].append(
        {
            "key": "wa",
            "type": "whatsapp_evolution",
            "name": "WA",
            "agent": "main",
            "config": {"base_url": "https://evo.example.com", "instance": "x"},
            "secret": {"secret": "evo_key"},
        }
    )
    for dry_run in (True, False):
        response = await _apply(client, headers, data, dry_run=dry_run)
        assert response.status_code == 422
        assert response.json()["error"]["details"] == [
            {"field": "tools[0].auth_config.token", "message": "secret 'orders_token' is not set"},
            {"field": "channels[2].secret", "message": "secret 'evo_key' is not set"},
        ]
    assert await _count(db_session, ManifestItem, tenant_id) == 0
    assert await _count(db_session, Channel, tenant_id) == 0


async def test_a_plain_string_credential_is_refused_without_echoing_it(client, db_session):
    headers, tenant_id = await _tenant(client)
    data = _manifest()
    data["tools"][0]["auth_config"]["token"] = TOKEN
    data["channels"].append(
        {
            "key": "wa",
            "type": "whatsapp_evolution",
            "name": "WA",
            "agent": "main",
            "config": {"base_url": "https://evo.example.com", "instance": "x"},
            "secret": EVO_KEY,
        }
    )
    response = await _apply(client, headers, data)
    assert response.status_code == 422
    assert response.json()["error"]["details"] == [
        {"field": "tools[0].auth_config", "message": CREDENTIALS_MESSAGE},
        {"field": "channels[2].secret", "message": CREDENTIALS_MESSAGE},
    ]
    assert await _count(db_session, ManifestItem, tenant_id) == 0


async def test_replacing_a_secret_makes_the_next_apply_an_update(client, db_session):
    headers, tenant_id = await _tenant(client)
    data = _manifest()
    data["channels"].append(
        {
            "key": "wa",
            "type": "whatsapp_evolution",
            "name": "WA",
            "agent": "main",
            "config": {"base_url": "https://evo.example.com", "instance": "x"},
            "secret": {"secret": "evo_key"},
        }
    )
    await _put(client, headers, "orders_token", TOKEN)
    await _put(client, headers, "evo_key", EVO_KEY)
    assert (await _apply(client, headers, data)).status_code == 200
    assert {c["action"] for c in (await _apply(client, headers, data)).json()["changes"]} == {
        "unchanged"
    }

    await _put(client, headers, "orders_token", TOKEN_2)
    await _put(client, headers, "evo_key", TOKEN_2)
    response = await _apply(client, headers, data)
    actions = _actions(response.json())
    assert actions[("tool", "get_order")] == ("update", ["auth_config"])
    assert actions[("channel", "wa")] == ("update", ["secret"])
    assert actions[("channel", "site")] == ("unchanged", [])

    tool = await db_session.scalar(select(Tool).where(Tool.tenant_id == tenant_id))
    await db_session.refresh(tool)
    assert json.loads(secrets_crypto.decrypt(tool.auth_config_encrypted))["token"] == TOKEN_2
    wa = await _channel(db_session, tenant_id, "wa")
    assert secrets_crypto.decrypt(wa.secret_encrypted) == TOKEN_2
    assert {c["action"] for c in (await _apply(client, headers, data)).json()["changes"]} == {
        "unchanged"
    }


async def test_a_private_events_url_is_refused_as_the_api_refuses(client, db_session):
    headers, tenant_id = await _tenant(client)
    await _put(client, headers, "orders_token", TOKEN)
    agent = await client.post(
        "/agents",
        json={"name": "A", "instructions": "x", "model": "gpt-4o-mini"},
        headers=headers,
    )
    api = await client.post(
        "/channels",
        json={
            "type": "external",
            "name": "I",
            "agent_id": agent.json()["id"],
            "config": {"events_url": "http://10.0.0.5/hook"},
        },
        headers=headers,
    )
    assert api.status_code == 422
    data = _manifest()
    data["channels"][1]["config"] = {"events_url": "http://10.0.0.5/hook"}
    response = await _apply(client, headers, data)
    assert response.status_code == 422
    assert response.json()["error"]["details"] == [
        {"field": "channels[1].config", "message": api.json()["error"]["message"]}
    ]
    assert await _count(db_session, Channel, tenant_id) == 0


async def test_channel_rules(client, db_session):
    headers, tenant_id = await _tenant(client)
    await _put(client, headers, "orders_token", TOKEN)
    data = _manifest()
    data["channels"][0]["agent"] = "nobody"
    data["channels"][1]["secret"] = {"secret": "orders_token"}
    data["channels"].append({**data["channels"][0], "agent": "main"})  # duplicate key
    response = await _apply(client, headers, data)
    assert response.status_code == 422
    assert response.json()["error"]["details"] == [
        {"field": "channels[2].key", "message": "duplicate key in the file"},
        {"field": "channels[0].agent", "message": "unknown agent: not an agent key in this file"},
        {
            "field": "channels[1].secret",
            "message": "a secret is only allowed on a whatsapp_evolution channel",
        },
    ]

    assert (await _apply(client, headers, _manifest())).status_code == 200
    changed = _manifest()
    changed["channels"][1] = {**changed["channels"][1], "type": "webchat", "config": {}}
    response = await _apply(client, headers, changed)
    assert response.status_code == 422
    assert response.json()["error"]["details"] == [
        {"field": "channels[1].type", "message": "type cannot change; use a new key"}
    ]
    inbox = await _channel(db_session, tenant_id, "inbox")
    assert inbox.type == ChannelType.EXTERNAL

    # config is declarative: the file's config replaces the stored one.
    renamed = _manifest()
    renamed["channels"][1]["name"] = "Inbox 2"
    renamed["channels"][1]["config"] = {"events_url": EVENTS_URL, "humanize": True}
    response = await _apply(client, headers, renamed)
    assert _actions(response.json())[("channel", "inbox")] == ("update", ["name", "config"])
    assert response.json()["generated"] == {}
    inbox = await _channel(db_session, tenant_id, "inbox")
    assert inbox.config == {"events_url": EVENTS_URL, "humanize": True}


def _with_whatsapp(data: dict) -> dict:
    data["channels"].append(
        {
            "key": "wa",
            "type": "whatsapp_evolution",
            "name": "WA",
            "agent": "main",
            "config": {"base_url": "https://evo.example.com", "instance": "x"},
            "secret": {"secret": "evo_key"},
        }
    )
    return data


async def test_the_export_keeps_the_secret_names_the_file_used(client, db_session):
    headers, tenant_id = await _tenant(client)
    await _put(client, headers, "orders_token", TOKEN)
    await _put(client, headers, "evo_key", EVO_KEY)
    data = _with_whatsapp(_manifest())
    assert (await _apply(client, headers, data)).status_code == 200

    stored = {
        key: names
        for key, names in (
            await db_session.execute(
                select(ManifestItem.key, ManifestItem.secret_names).where(
                    ManifestItem.tenant_id == tenant_id,
                    ManifestItem.kind.in_(("tool", "channel")),
                )
            )
        ).all()
    }
    assert stored == {
        "get_order": {"token": "orders_token"},
        "site": {},
        "inbox": {},
        "wa": {"secret": "evo_key"},
    }

    exported = (await client.get("/manifest", headers=headers)).json()
    assert exported["secrets_needed"] == []
    tool = exported["manifest"]["tools"][0]
    assert tool["auth_config"]["token"] == {"secret": "orders_token"}
    wa = next(c for c in exported["manifest"]["channels"] if c["key"] == "wa")
    assert wa["secret"] == {"secret": "evo_key"}

    response = await _apply(client, headers, exported["manifest"], dry_run=True)
    assert response.status_code == 200, response.text
    assert {a for a, _ in _actions(response.json()).values()} == {"unchanged"}


async def test_a_renamed_secret_in_the_file_is_remembered_on_an_unchanged_item(
    client, db_session
):
    headers, tenant_id = await _tenant(client)
    await _put(client, headers, "orders_token", TOKEN)
    await _put(client, headers, "orders_token_v2", TOKEN)
    data = _manifest()
    assert (await _apply(client, headers, data)).status_code == 200
    data["tools"][0]["auth_config"]["token"] = {"secret": "orders_token_v2"}
    response = await _apply(client, headers, data)
    assert _actions(response.json())[("tool", "get_order")] == ("unchanged", [])
    exported = (await client.get("/manifest", headers=headers)).json()
    assert exported["manifest"]["tools"][0]["auth_config"]["token"] == {
        "secret": "orders_token_v2"
    }


async def test_a_secret_in_use_cannot_be_deleted_but_can_be_replaced(client, db_session):
    headers, _ = await _tenant(client)
    await _put(client, headers, "orders_token", TOKEN)
    await _put(client, headers, "evo_key", EVO_KEY)
    await _put(client, headers, "unused", TOKEN_2)
    data = _with_whatsapp(_manifest())
    data["channels"][-1]["secret"] = {"secret": "orders_token"}
    assert (await _apply(client, headers, data)).status_code == 200

    refused = await client.delete("/secrets/orders_token", headers=headers)
    assert refused.status_code == 409
    assert refused.json()["error"]["message"] == "secret is used by tool 'get_order', channel 'wa'"
    listed = (await client.get("/secrets", headers=headers)).json()
    assert "orders_token" in {s["name"] for s in listed}

    await _put(client, headers, "orders_token", TOKEN_2)  # replacing stays allowed
    assert (await client.delete("/secrets/unused", headers=headers)).status_code == 204
    assert (await client.delete("/secrets/evo_key", headers=headers)).status_code == 204

    # Once the file stops using it, the delete goes through.
    data["tools"][0]["auth_config"] = None
    data["channels"] = data["channels"][:2]
    assert (await _apply(client, headers, data)).status_code == 200
    assert (await client.delete("/secrets/orders_token", headers=headers)).status_code == 204
