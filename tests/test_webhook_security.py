"""E2.4: webhook tenant/channel come from the authenticated channel, never the payload."""

import copy
import json
import uuid
from pathlib import Path

from sqlalchemy import select

from app.models.message import Message
from tests.test_ingest import _auth, _create_tenant

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "evolution_messages_upsert.json"


async def _create_evolution_channel(client, headers: dict, secret: str) -> str:
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
            "secret": secret,
        },
        headers=headers,
    )
    assert channel.status_code == 201
    return channel.json()["id"]


async def test_webhook_ignores_foreign_tenant_and_channel_in_payload(
    client, fake_redis, db_session
):
    victim = await _create_tenant(client, "Victim")
    attacker = await _create_tenant(client, "Attacker")
    victim_channel = await _create_evolution_channel(client, _auth(victim["api_key"]), "victim-s")
    attacker_channel = await _create_evolution_channel(
        client, _auth(attacker["api_key"]), "attacker-s"
    )

    payload = copy.deepcopy(json.loads(FIXTURE_PATH.read_text()))
    payload["tenant_id"] = victim["id"]
    payload["channel_id"] = victim_channel

    response = await client.post(
        f"/webhooks/whatsapp/{attacker_channel}",
        json=payload,
        headers={"apikey": "attacker-s"},
    )
    assert response.status_code == 200

    rows = (
        await db_session.execute(
            select(Message).where(Message.tenant_id == uuid.UUID(attacker["id"]))
        )
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].tenant_id == uuid.UUID(attacker["id"])
    assert rows[0].channel_id == uuid.UUID(attacker_channel)

    foreign = (
        await db_session.execute(
            select(Message).where(
                (Message.tenant_id == uuid.UUID(victim["id"]))
                | (Message.channel_id == uuid.UUID(victim_channel))
            )
        )
    ).scalars().all()
    assert foreign == []
