"""Evolution ingests only people: groups, broadcasts and other non-person JIDs
are ignored; `@lid` senders keep the full JID and are addressed at it."""

import copy
import json
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import respx
from sqlalchemy import func, select

from app.channels.base import OutgoingMessage
from app.channels.evolution import EvolutionAdapter, IgnoredEvent
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.message import Message
from app.queue.redis_queue import JOBS_KEY
from tests.test_evolution import _auth, _create_evolution_channel, _create_tenant

FIXTURES = Path(__file__).parent / "fixtures"
BASE = "https://evolution.example.com"
GET_BASE64_URL = f"{BASE}/chat/getBase64FromMediaMessage/harness-instance"


def _payload(jid: str, fixture: str = "evolution_messages_upsert.json") -> dict:
    payload = copy.deepcopy(json.loads((FIXTURES / fixture).read_text()))
    payload["data"]["key"]["remoteJid"] = jid
    payload["tenant_id"] = "00000000-0000-0000-0000-000000000001"
    payload["channel_id"] = "00000000-0000-0000-0000-000000000002"
    return payload


def _channel():
    return SimpleNamespace(
        id="c",
        tenant_id="t",
        secret_encrypted=None,
        config={"base_url": BASE, "instance": "harness-instance"},
    )


def test_phone_jid_strips_suffix():
    incoming = EvolutionAdapter().parse_incoming(_payload("5511999998888@s.whatsapp.net"))
    assert incoming.external_user_id == "5511999998888"


def test_lid_jid_keeps_full_id():
    incoming = EvolutionAdapter().parse_incoming(_payload("1234567890@lid"))
    assert incoming.external_user_id == "1234567890@lid"


def test_bare_number_accepted_as_is():
    incoming = EvolutionAdapter().parse_incoming(_payload("5511999998888"))
    assert incoming.external_user_id == "5511999998888"


@pytest.mark.parametrize(
    "jid",
    [
        "120363025@g.us",
        "status@broadcast",
        "5511999998888@broadcast",
        "123@newsletter",
        "",
        "5511999998888@unknown.suffix",
    ],
)
def test_non_person_jids_are_ignored(jid):
    with pytest.raises(IgnoredEvent):
        EvolutionAdapter().parse_incoming(_payload(jid))


def test_missing_remote_jid_is_ignored():
    payload = _payload("x@g.us")
    del payload["data"]["key"]["remoteJid"]
    with pytest.raises(IgnoredEvent):
        EvolutionAdapter().parse_incoming(payload)


def test_unknown_suffix_logs_suffix_only():
    from structlog.testing import capture_logs

    with capture_logs() as logs, pytest.raises(IgnoredEvent):
        EvolutionAdapter().parse_incoming(_payload("5511999998888@weird.suffix"))
    warnings = [e for e in logs if e["log_level"] == "warning"]
    assert warnings
    assert "5511999998888" not in json.dumps(warnings, default=str)
    assert "weird.suffix" in json.dumps(warnings, default=str)


async def _counts(db_session, tenant_id: str) -> tuple[int, int, int]:
    tid = uuid.UUID(tenant_id)
    return tuple(
        [
            (
                await db_session.execute(
                    select(func.count()).select_from(model).where(model.tenant_id == tid)
                )
            ).scalar_one()
            for model in (Contact, Conversation, Message)
        ]
    )


@respx.mock
@pytest.mark.parametrize(
    "jid, fixture",
    [
        ("120363025@g.us", "evolution_messages_upsert.json"),
        ("status@broadcast", "evolution_messages_upsert.json"),
        ("120363025@g.us", "evolution_image_upsert.json"),
        ("status@broadcast", "evolution_audio_upsert.json"),
    ],
)
async def test_webhook_ignores_non_person(client, fake_redis, db_session, jid, fixture):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    media_route = respx.post(GET_BASE64_URL).mock(return_value=httpx.Response(200, json={}))

    payload = _payload(jid, fixture)
    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}", json=payload, headers={"apikey": "evo-secret-xyz"}
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert await _counts(db_session, tenant["id"]) == (0, 0, 0)
    assert await fake_redis.llen(JOBS_KEY) == 0
    assert not media_route.called


async def test_webhook_lid_sender_stored_and_addressed_at_lid(client, fake_redis, db_session):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))

    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_payload("1234567890@lid"),
        headers={"apikey": "evo-secret-xyz"},
    )
    assert response.status_code == 200
    assert response.json() != {"status": "ignored"}
    contact = (
        await db_session.execute(
            select(Contact).where(Contact.tenant_id == uuid.UUID(tenant["id"]))
        )
    ).scalar_one()
    assert contact.external_user_id == "1234567890@lid"
    assert (await _counts(db_session, tenant["id"]))[2] == 1
    assert await fake_redis.llen(JOBS_KEY) == 1


async def test_webhook_phone_sender_unchanged(client, fake_redis, db_session):
    tenant = await _create_tenant(client)
    channel_id = await _create_evolution_channel(client, _auth(tenant["api_key"]))
    response = await client.post(
        f"/webhooks/whatsapp/{channel_id}",
        json=_payload("5511999998888@s.whatsapp.net"),
        headers={"apikey": "evo-secret-xyz"},
    )
    assert response.status_code == 200
    contact = (
        await db_session.execute(
            select(Contact).where(Contact.tenant_id == uuid.UUID(tenant["id"]))
        )
    ).scalar_one()
    assert contact.external_user_id == "5511999998888"


@respx.mock
@pytest.mark.parametrize(
    "external_id, expected_jid",
    [
        ("1234567890@lid", "1234567890@lid"),
        ("5511999998888", "5511999998888@s.whatsapp.net"),
    ],
)
async def test_outbound_calls_address_the_same_person(external_id, expected_jid):
    send = respx.post(f"{BASE}/message/sendText/harness-instance").mock(
        return_value=httpx.Response(200, json={})
    )
    typing = respx.post(f"{BASE}/chat/sendPresence/harness-instance").mock(
        return_value=httpx.Response(200, json={})
    )
    read = respx.post(f"{BASE}/chat/markMessageAsRead/harness-instance").mock(
        return_value=httpx.Response(200, json={})
    )
    contact = SimpleNamespace(external_user_id=external_id)
    adapter = EvolutionAdapter()

    await adapter.send_message(
        OutgoingMessage(conversation_id=uuid.uuid4(), content="oi"), contact, _channel()
    )
    await adapter.send_typing(contact, _channel(), 1.0)
    await adapter.mark_read("MSG1", contact, _channel())

    assert json.loads(send.calls.last.request.content)["number"] == external_id
    assert json.loads(typing.calls.last.request.content)["number"] == external_id
    body = json.loads(read.calls.last.request.content)
    assert body["readMessages"][0]["remoteJid"] == expected_jid
