"""The success demo of the manifest milestone, as one scenario: a clinic is
set up with the secrets and manifest endpoints only, answers a customer,
changes by editing the file, and exports to a file that changes nothing."""

import os
import time
import uuid
from contextlib import asynccontextmanager

from sqlalchemy import select

import app.agents.categorize as categorize_module
from app.agents.runtime import AgentRuntime
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.agent import Agent
from app.models.category import Category
from app.models.channel import Channel
from app.models.message import Message, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.workers.main import run_once
from app.workers.scheduler import tick

SECRET_VALUE = "agenda-key-9f3c1d7e2b"
PERSONA = "Você é a recepcionista da Clínica Vida. Marque consultas com clareza."
NEW_PERSONA = "Você é a recepcionista da Clínica Vida. Responda sempre em duas frases."

CLINIC_YAML = f"""\
version: 1
agents:
  - key: recepcao
    name: Recepção Clínica Vida
    model: gpt-4o-mini
    instructions: {PERSONA}
    tool_names:
      - schedule_appointment
departments:
  - name: cardiologia
  - name: dermatologia
  - name: pediatria
tools:
  - name: schedule_appointment
    description: Marca uma consulta na agenda da clínica.
    input_schema:
      type: object
      properties:
        patient: {{type: string}}
      required: [patient]
    endpoint: https://agenda.example.com/appointments
    method: POST
    auth_config:
      type: bearer
      token:
        secret: clinic_agenda_key
channels:
  - key: site
    type: webchat
    name: Site da clínica
    agent: recepcao
"""

KINDS_AND_KEYS = [
    ("department", "cardiologia"),
    ("department", "dermatologia"),
    ("department", "pediatria"),
    ("tool", "schedule_appointment"),
    ("agent", "recepcao"),
    ("channel", "site"),
]
# Setup after the tenant exists may call only these (method, path) pairs.
SETUP_ALLOWED = {
    ("PUT", "/secrets/clinic_agenda_key"),
    ("POST", "/manifest/apply"),
    ("GET", "/manifest"),
}


def _session_factory(db_session):
    @asynccontextmanager
    async def factory():
        yield db_session

    return factory


def _by_key(body) -> dict[tuple[str, str], tuple[str, list[str]]]:
    return {(c["kind"], c["key"]): (c["action"], c["fields"]) for c in body["changes"]}


async def _answered(client, fake_redis, db_session, headers, channel_id, user, reply):
    """A customer message on the channel, the turn run with a scripted model;
    returns (reply text stored, the provider with its recorded calls)."""
    sent = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": str(channel_id), "user_id": user, "message": "Quero marcar consulta"},
        headers=headers,
    )
    assert sent.status_code == 201, sent.text
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=0.2) is not None
    provider = FakeLLMProvider([LLMResponse(content=reply)])
    await tick(
        fake_redis,
        _session_factory(db_session),
        AgentRuntime(provider=provider),
        now=time.time() + 5,
    )
    assert provider.call_count >= 1
    return provider


async def _last_reply(db_session, tenant_id) -> str | None:
    return await db_session.scalar(
        select(Message.content)
        .where(Message.tenant_id == tenant_id, Message.role == MessageRole.ASSISTANT)
        .order_by(Message.created_at.desc())
        .limit(1)
    )


def _system_text(provider: FakeLLMProvider) -> str:
    first = provider.calls[0]["messages"][0]
    content = first.content if hasattr(first, "content") else first["content"]
    return content if isinstance(content, str) else str(content)


async def test_the_clinic_is_set_up_changed_and_exported_with_the_manifest_alone(
    client, fake_redis, db_session, monkeypatch
):
    # The department classifier is scripted too: it files no conversation.
    classifier = FakeLLMProvider([LLMResponse(content="0")], repeat_last=True)
    monkeypatch.setattr(categorize_module, "get_provider", lambda agent, settings=None: classifier)

    # 1. The tenant, then only secrets and manifest calls for setup.
    created = await client.post(
        "/tenants", json={"name": "Clínica Vida"},
        headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]},
    )
    assert created.status_code == 201, created.text
    tenant_id = uuid.UUID(created.json()["id"])
    headers = {"X-API-Key": created.json()["api_key"]}
    setup_calls: list[tuple[str, str]] = []

    async def setup(method: str, path: str, **kwargs):
        setup_calls.append((method, path.split("?")[0]))
        extra = kwargs.pop("headers", {})
        response = await client.request(method, path, headers={**headers, **extra}, **kwargs)
        assert SECRET_VALUE not in response.text
        return response

    async def apply_yaml(text: str, *, dry_run: bool):
        return await setup(
            "POST",
            f"/manifest/apply?dry_run={'true' if dry_run else 'false'}",
            content=text.encode(),
            headers={"Content-Type": "application/yaml"},
        )

    # 2. Dry run before the secret is set: 422 names the missing secret.
    refused = await apply_yaml(CLINIC_YAML, dry_run=True)
    assert refused.status_code == 422, refused.text
    assert refused.json()["error"]["details"] == [
        {"field": "tools[0].auth_config.token", "message": "secret 'clinic_agenda_key' is not set"}
    ]
    stored = await setup("PUT", "/secrets/clinic_agenda_key", json={"value": SECRET_VALUE})
    assert stored.status_code == 204

    # 3. Dry run: everything is a create, and nothing exists yet.
    empty = (await setup("GET", "/manifest")).json()
    assert empty["manifest"] == {"version": 1}
    dry = await apply_yaml(CLINIC_YAML, dry_run=True)
    assert dry.status_code == 200, dry.text
    assert dry.json()["dry_run"] is True
    plan = [(c["kind"], c["key"], c["action"], c["fields"]) for c in dry.json()["changes"]]
    assert {(k, key) for k, key, _, _ in plan} == set(KINDS_AND_KEYS)
    assert all((action, fields) == ("create", []) for _, _, action, fields in plan)
    assert (await setup("GET", "/manifest")).json()["manifest"] == {"version": 1}

    # 4. Apply: the same list; the channel answers; the persona is stored.
    applied = await apply_yaml(CLINIC_YAML, dry_run=False)
    assert applied.status_code == 200, applied.text
    assert applied.json()["dry_run"] is False
    assert [
        (c["kind"], c["key"], c["action"], c["fields"]) for c in applied.json()["changes"]
    ] == plan
    agent = await db_session.scalar(select(Agent).where(Agent.tenant_id == tenant_id))
    assert agent.instructions == PERSONA
    channel_id = await db_session.scalar(select(Channel.id).where(Channel.tenant_id == tenant_id))
    provider = await _answered(
        client, fake_redis, db_session, headers, channel_id, "paciente-1", "Claro! Qual dia?"
    )
    assert PERSONA in _system_text(provider)
    assert await _last_reply(db_session, tenant_id) == "Claro! Qual dia?"

    # 5. Edit the file: one department out, a new persona.
    edited = CLINIC_YAML.replace("  - name: pediatria\n", "").replace(PERSONA, NEW_PERSONA)
    assert edited != CLINIC_YAML and "pediatria" not in edited
    dry = await apply_yaml(edited, dry_run=True)
    assert dry.status_code == 200, dry.text
    changes = _by_key(dry.json())
    assert changes[("department", "pediatria")] == ("delete", [])
    assert changes[("agent", "recepcao")] == ("update", ["instructions"])
    assert {k: v for k, v in changes.items() if v[0] not in ("delete", "update")} == {
        key: ("unchanged", [])
        for key in KINDS_AND_KEYS
        if key not in (("department", "pediatria"), ("agent", "recepcao"))
    }
    assert len(changes) == len(KINDS_AND_KEYS)
    assert (await apply_yaml(edited, dry_run=False)).status_code == 200

    # 6. A third apply, as a dry run, has nothing to do; the change took effect.
    third = await apply_yaml(edited, dry_run=True)
    assert third.status_code == 200, third.text
    assert set(_by_key(third.json())) == set(KINDS_AND_KEYS) - {("department", "pediatria")}
    assert {a for a, _ in _by_key(third.json()).values()} == {"unchanged"}
    names = set(
        await db_session.scalars(select(Category.name).where(Category.tenant_id == tenant_id))
    )
    assert names == {"cardiologia", "dermatologia"}
    await db_session.refresh(agent)
    assert agent.instructions == NEW_PERSONA
    provider = await _answered(
        client, fake_redis, db_session, headers, channel_id, "paciente-2", "Dia 10, às 9h."
    )
    assert NEW_PERSONA in _system_text(provider)
    assert await _last_reply(db_session, tenant_id) == "Dia 10, às 9h."
    assert PERSONA not in _system_text(provider)

    # 7. Export as YAML; applying it as a dry run changes nothing; no secret value.
    exported = await setup("GET", "/manifest?format=yaml")
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/yaml")
    assert exported.text.startswith("# secrets_needed: none\n")
    assert SECRET_VALUE not in exported.text
    assert "clinic_agenda_key" in exported.text  # the reference, never the value
    again = await apply_yaml(exported.text, dry_run=True)
    assert again.status_code == 200, again.text
    assert {a for a, _ in _by_key(again.json()).values()} == {"unchanged"}
    assert len(again.json()["changes"]) == len(KINDS_AND_KEYS) - 1

    # Setup used only the secrets and manifest endpoints.
    assert set(setup_calls) <= SETUP_ALLOWED, set(setup_calls) - SETUP_ALLOWED
    assert {path for _, path in setup_calls} == {p for _, p in SETUP_ALLOWED}
