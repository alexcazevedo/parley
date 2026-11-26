"""PRODUCT.md Success demo for the follow-up security run, on a non-demo app.

One walk: an Evolution channel at a private `base_url` is refused at create
and at update, and the audit lists exactly the rows an older version could
have left behind (a private `base_url`, a conversation on another tenant's
agent). The call-time refusal, the media cap and the refused turn have their
own tests (`test_channel_ssrf.py`, `test_evolution_media.py`,
`test_agent_tenant_scope.py`); the other halves of the demo are proven there.
"""

import os
import uuid

import pytest_asyncio
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from app.api.deps import get_db, get_redis
from app.audit import find_offending_rows
from app.config import Settings
from app.main import create_app
from app.models.channel import Channel
from app.models.contact import Contact
from app.models.conversation import Conversation

PRIVATE_URL = "http://10.0.0.5:8080"
PUBLIC_URL = "https://evo.example.com"


@pytest_asyncio.fixture
async def secure_client(db_session, fake_redis):
    settings = Settings(
        demo_mode=False,
        admin_api_key="k",
        secrets_key=Fernet.generate_key().decode(),
    )
    secure_app = create_app(settings)

    async def _db():
        yield db_session

    async def _redis():
        return fake_redis

    secure_app.dependency_overrides[get_db] = _db
    secure_app.dependency_overrides[get_redis] = _redis
    async with AsyncClient(transport=ASGITransport(app=secure_app), base_url="http://test") as ac:
        yield ac


async def _tenant(ac) -> tuple[uuid.UUID, dict]:
    # Auth reads the admin key from get_settings() (env), not the Settings
    # passed to create_app.
    r = await ac.post(
        "/tenants",
        json={"name": f"T-{uuid.uuid4().hex[:8]}"},
        headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    return uuid.UUID(body["id"]), {"X-API-Key": body["api_key"]}


async def _agent(ac, headers) -> uuid.UUID:
    r = await ac.post(
        "/agents",
        json={"name": "Bot", "instructions": "Hi.", "model": "gpt-4o-mini"},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return uuid.UUID(r.json()["id"])


def _evolution(agent_id: uuid.UUID, base_url: str) -> dict:
    return {
        "type": "whatsapp_evolution",
        "name": "Evo",
        "agent_id": str(agent_id),
        "config": {"base_url": base_url, "instance": "i"},
        "secret": "evolution-apikey",
    }


async def test_success_demo_on_non_demo_app(secure_client, db_session):
    ac = secure_client
    tenant_a, headers_a = await _tenant(ac)
    tenant_b, headers_b = await _tenant(ac)
    agent_a = await _agent(ac, headers_a)
    agent_b = await _agent(ac, headers_b)

    # Refused at create.
    r = await ac.post("/channels", json=_evolution(agent_b, PRIVATE_URL), headers=headers_b)
    assert r.status_code == 422, r.text
    assert r.json()["error"]["message"].startswith("invalid base_url")

    # Refused at update.
    ok = await ac.post("/channels", json=_evolution(agent_b, PUBLIC_URL), headers=headers_b)
    assert ok.status_code == 201, ok.text
    evo_id = uuid.UUID(ok.json()["id"])
    r = await ac.patch(
        f"/channels/{evo_id}",
        json={"config": {"base_url": PRIVATE_URL, "instance": "i"}},
        headers=headers_b,
    )
    assert r.status_code == 422, r.text
    assert r.json()["error"]["message"].startswith("invalid base_url")

    # Rows an older version could have written: a channel forced onto the
    # private URL, and a conversation forced onto tenant A's agent.
    forced = await ac.post("/channels", json=_evolution(agent_b, PUBLIC_URL), headers=headers_b)
    forced_id = uuid.UUID(forced.json()["id"])
    channel = await db_session.get(Channel, forced_id)
    channel.config = {"base_url": PRIVATE_URL, "instance": "i"}

    web = await ac.post(
        "/channels",
        json={"type": "webchat", "name": "W", "agent_id": str(agent_b)},
        headers=headers_b,
    )
    web_id = uuid.UUID(web.json()["id"])
    contact = Contact(tenant_id=tenant_b, channel_id=web_id, external_user_id=uuid.uuid4().hex)
    db_session.add(contact)
    await db_session.flush()
    conversation = Conversation(
        tenant_id=tenant_b, contact_id=contact.id, channel_id=web_id, agent_id=agent_a
    )
    db_session.add(conversation)
    await db_session.commit()

    findings = await find_offending_rows(db_session)
    by_id = {f.row_id: f for f in findings}

    assert by_id[forced_id].kind == "channel_refused_base_url"
    assert by_id[forced_id].tenant_id == tenant_b
    assert by_id[conversation.id].kind == "conversation_foreign_agent"
    assert by_id[conversation.id].detail == f"agent={agent_a}"
    # The clean channels are not reported.
    assert evo_id not in by_id
    assert web_id not in by_id
