"""Read-only audit of rows an older version could have left behind (PRODUCT.md F4)."""

import asyncio
import os
import socket
import uuid

from sqlalchemy import func, select

import app.audit as audit
from app.audit import Finding, find_offending_rows
from app.config import get_settings
from app.models.agent import Agent
from app.models.channel import Channel, ChannelType
from app.models.contact import Contact
from app.models.conversation import Conversation

SECRET = "super-secret-token-do-not-print"
APIKEY = "evolution-apikey-do-not-print"


async def _tenant(client) -> tuple[uuid.UUID, dict]:
    response = await client.post(
        "/tenants",
        json={"name": "Audit"},
        headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    return uuid.UUID(body["id"]), {"X-API-Key": body["api_key"]}


async def _agent(client, headers) -> uuid.UUID:
    response = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "x", "model": "gpt-4o-mini"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _channel(db, tenant_id, agent_id, *, type_=ChannelType.WEBCHAT, config=None):
    channel = Channel(
        tenant_id=tenant_id,
        type=type_,
        name="c",
        agent_id=agent_id,
        config=config or {},
        secret_encrypted=SECRET,
    )
    db.add(channel)
    await db.flush()
    return channel


async def _conversation(db, tenant_id, channel_id, agent_id) -> Conversation:
    contact = Contact(
        tenant_id=tenant_id, channel_id=channel_id, external_user_id=uuid.uuid4().hex
    )
    db.add(contact)
    await db.flush()
    conversation = Conversation(
        tenant_id=tenant_id, contact_id=contact.id, channel_id=channel_id, agent_id=agent_id
    )
    db.add(conversation)
    await db.flush()
    return conversation


def _evo(base_url) -> dict:
    return {"base_url": base_url, "instance": "i", "apikey": APIKEY}


async def _seed(client, db):
    tenant_a, headers_a = await _tenant(client)
    tenant_b, headers_b = await _tenant(client)
    agent_a = await _agent(client, headers_a)
    agent_b = await _agent(client, headers_b)

    clean_channel = await _channel(db, tenant_b, agent_b)
    clean_conv = await _conversation(db, tenant_b, clean_channel.id, agent_b)
    clean_evo = await _channel(
        db,
        tenant_b,
        agent_b,
        type_=ChannelType.WHATSAPP_EVOLUTION,
        config=_evo("https://evo.example.com"),
    )
    bad_channel = await _channel(db, tenant_b, agent_a)
    bad_conv = await _conversation(db, tenant_b, clean_channel.id, agent_a)
    bad_url = await _channel(
        db,
        tenant_b,
        agent_b,
        type_=ChannelType.WHATSAPP_EVOLUTION,
        config=_evo("http://evo.internal:8080"),
    )
    await db.commit()
    return {
        "tenant_b": tenant_b,
        "agent_a": agent_a,
        "clean": {clean_channel.id, clean_conv.id, clean_evo.id},
        "bad_channel": bad_channel.id,
        "bad_conv": bad_conv.id,
        "bad_url": bad_url.id,
    }


def _by(findings: list[Finding], kind: str) -> dict[uuid.UUID, Finding]:
    return {f.row_id: f for f in findings if f.kind == kind}


async def test_finds_each_kind_and_skips_clean_rows(client, db_session):
    seeded = await _seed(client, db_session)
    findings = await find_offending_rows(db_session)

    channels = _by(findings, "channel_foreign_agent")
    convs = _by(findings, "conversation_foreign_agent")
    urls = _by(findings, "channel_refused_base_url")

    assert channels[seeded["bad_channel"]].tenant_id == seeded["tenant_b"]
    assert str(seeded["agent_a"]) in channels[seeded["bad_channel"]].detail
    assert convs[seeded["bad_conv"]].tenant_id == seeded["tenant_b"]
    assert urls[seeded["bad_url"]].tenant_id == seeded["tenant_b"]
    assert "evo.internal" in urls[seeded["bad_url"]].detail

    assert seeded["bad_url"] not in channels
    assert seeded["bad_channel"] not in urls
    assert not seeded["clean"] & {f.row_id for f in findings}


async def test_allowed_host_stops_being_reported(client, db_session, monkeypatch):
    seeded = await _seed(client, db_session)
    before = _by(await find_offending_rows(db_session), "channel_refused_base_url")
    assert seeded["bad_url"] in before

    monkeypatch.setenv("CHANNEL_ALLOWED_HOSTS", "evo.internal")
    get_settings.cache_clear()
    try:
        findings = await find_offending_rows(db_session)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    assert seeded["bad_url"] not in _by(findings, "channel_refused_base_url")


async def test_non_string_base_url_is_reported_not_raised(client, db_session):
    tenant, headers = await _tenant(client)
    agent = await _agent(client, headers)
    channel = await _channel(
        db_session, tenant, agent, type_=ChannelType.WHATSAPP_EVOLUTION, config=_evo(12345)
    )
    await db_session.commit()
    findings = await find_offending_rows(db_session)
    assert channel.id in _by(findings, "channel_refused_base_url")


async def test_audit_changes_nothing(client, db_session):
    await _seed(client, db_session)

    async def counts():
        return [
            (await db_session.execute(select(func.count()).select_from(model))).scalar_one()
            for model in (Channel, Conversation, Agent)
        ]

    before = await counts()
    await find_offending_rows(db_session)
    assert not db_session.new and not db_session.dirty and not db_session.deleted
    await db_session.rollback()
    assert await counts() == before


async def test_main_exit_code_and_output(client, db_session, capsys):
    seeded = await _seed(client, db_session)
    # main() owns its event loop, so run it off this one.
    code = await asyncio.to_thread(audit.main)
    out = capsys.readouterr().out
    lines = out.strip().splitlines()

    assert code == 1
    assert lines[-1].endswith(" offending rows")
    assert int(lines[-1].split()[0]) == len(lines) - 1
    assert (
        f"channel_foreign_agent {seeded['bad_channel']} tenant={seeded['tenant_b']}" in out
    )
    assert f"conversation_foreign_agent {seeded['bad_conv']} tenant=" in out
    assert f"channel_refused_base_url {seeded['bad_url']} tenant=" in out
    for clean_id in seeded["clean"]:
        assert str(clean_id) not in out
    assert SECRET not in out and APIKEY not in out


async def test_main_returns_zero_without_findings(monkeypatch, capsys):
    async def none(session, *, resolve=False):
        return []

    monkeypatch.setattr(audit, "find_offending_rows", none)
    assert await asyncio.to_thread(audit.main) == 0
    assert capsys.readouterr().out.strip().splitlines()[-1] == "0 offending rows"


# --- --resolve: private hostnames -----------------------------------------------


def _fake_dns(monkeypatch, address: str) -> list[str]:
    calls: list[str] = []

    def fake_getaddrinfo(host, port, *args, **kwargs):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return calls


async def _hostname_channel(client, db_session, base_url):
    tenant, headers = await _tenant(client)
    agent = await _agent(client, headers)
    channel = await _channel(
        db_session, tenant, agent, type_=ChannelType.WHATSAPP_EVOLUTION, config=_evo(base_url)
    )
    await db_session.commit()
    return channel


async def test_private_hostname_listed_only_with_resolve(client, db_session, monkeypatch):
    channel = await _hostname_channel(client, db_session, "http://evolution:8080")
    calls = _fake_dns(monkeypatch, "10.0.0.9")

    default = _by(await find_offending_rows(db_session), "channel_refused_base_url")
    assert channel.id not in default
    assert "evolution" not in calls  # no DNS for it without the flag

    resolved = _by(
        await find_offending_rows(db_session, resolve=True), "channel_refused_base_url"
    )
    assert channel.id in resolved
    assert "10.0.0.9" in resolved[channel.id].detail
    assert APIKEY not in resolved[channel.id].detail


async def test_public_hostname_not_listed_with_resolve(client, db_session, monkeypatch):
    channel = await _hostname_channel(client, db_session, "http://evolution:8080")
    _fake_dns(monkeypatch, "93.184.216.34")
    for resolve in (False, True):
        findings = await find_offending_rows(db_session, resolve=resolve)
        assert channel.id not in _by(findings, "channel_refused_base_url")


async def test_unresolvable_hostname_listed_with_resolve(client, db_session, monkeypatch):
    channel = await _hostname_channel(client, db_session, "http://evolution:8080")

    def fail(*args, **kwargs):
        raise socket.gaierror("nope")

    monkeypatch.setattr(socket, "getaddrinfo", fail)
    findings = await find_offending_rows(db_session, resolve=True)
    assert channel.id in _by(findings, "channel_refused_base_url")


async def test_allowed_host_not_listed_with_resolve(client, db_session, monkeypatch):
    channel = await _hostname_channel(client, db_session, "http://evolution:8080")
    calls = _fake_dns(monkeypatch, "10.0.0.9")
    monkeypatch.setenv("CHANNEL_ALLOWED_HOSTS", "evolution")
    get_settings.cache_clear()
    try:
        findings = await find_offending_rows(db_session, resolve=True)
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    assert channel.id not in _by(findings, "channel_refused_base_url")
    assert "evolution" not in calls  # other rows in the database may be resolved


async def test_main_accepts_resolve_flag(monkeypatch, capsys):
    seen: list[bool] = []

    async def fake(session, *, resolve=False):
        seen.append(resolve)
        return []

    monkeypatch.setattr(audit, "find_offending_rows", fake)
    assert await asyncio.to_thread(audit.main, ["--resolve"]) == 0
    assert await asyncio.to_thread(audit.main, []) == 0
    assert seen == [True, False]
