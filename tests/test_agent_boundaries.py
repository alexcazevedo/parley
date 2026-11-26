"""Agent boundary fields: API round-trip, validation limits, system prompt."""

import os
import uuid

import pytest

from app.agents.boundaries import (
    DEFAULT_REFUSAL_MESSAGE,
    Boundaries,
    effective_boundaries,
    render_boundaries,
)
from app.agents.context import ContextBuilder
from app.models.agent import Agent
from app.models.conversation import Conversation


async def _tenant_headers(client) -> dict:
    response = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    assert response.status_code == 201
    return {"X-API-Key": response.json()["api_key"]}


def _payload(**extra) -> dict:
    return {"name": "Bot", "instructions": "Be helpful.", "model": "gpt-4o-mini", **extra}


async def test_create_patch_and_read_back(client):
    headers = await _tenant_headers(client)
    created = await client.post("/agents", json=_payload(), headers=headers)
    assert created.status_code == 201
    body = created.json()
    assert body["boundary_scope"] is None
    assert body["boundary_never"] == []
    assert body["boundary_refusal_message"] is None
    assert body["boundary_check_enabled"] is True

    created = await client.post(
        "/agents",
        json=_payload(
            boundary_scope="clinic appointments",
            boundary_never=["medical advice"],
            boundary_refusal_message="No can do.",
            boundary_check_enabled=False,
        ),
        headers=headers,
    )
    assert created.status_code == 201
    agent_id = created.json()["id"]

    patched = await client.patch(
        f"/agents/{agent_id}",
        json={"boundary_never": ["medical advice", "prices"], "boundary_scope": "billing"},
        headers=headers,
    )
    assert patched.status_code == 200
    fetched = (await client.get(f"/agents/{agent_id}", headers=headers)).json()
    assert fetched["boundary_scope"] == "billing"
    assert fetched["boundary_never"] == ["medical advice", "prices"]
    assert fetched["boundary_refusal_message"] == "No can do."
    assert fetched["boundary_check_enabled"] is False


@pytest.mark.parametrize(
    "fields",
    [
        {"boundary_scope": "x" * 2001},
        {"boundary_never": ["a"] * 51},
        {"boundary_never": [""]},
        {"boundary_never": ["   "]},
        {"boundary_never": ["x" * 301]},
        {"boundary_refusal_message": ""},
        {"boundary_refusal_message": "x" * 1001},
    ],
)
async def test_limits_give_422(client, fields):
    headers = await _tenant_headers(client)
    response = await client.post("/agents", json=_payload(**fields), headers=headers)
    assert response.status_code == 422

    ok = await client.post("/agents", json=_payload(), headers=headers)
    patched = await client.patch(f"/agents/{ok.json()['id']}", json=fields, headers=headers)
    assert patched.status_code == 422


async def test_limits_boundaries_accepted(client):
    headers = await _tenant_headers(client)
    response = await client.post(
        "/agents",
        json=_payload(
            boundary_scope="x" * 2000,
            boundary_never=["a"] * 50,
            boundary_refusal_message="x" * 1000,
        ),
        headers=headers,
    )
    assert response.status_code == 201


def _agent(**overrides) -> Agent:
    defaults = dict(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        name="Bot",
        instructions="Você é o atendente da Acme.",
        model="gpt-4o-mini",
        context_message_limit=20,
    )
    return Agent(**{**defaults, **overrides})


def _conversation() -> Conversation:
    return Conversation(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        contact_id=uuid.uuid4(),
        channel_id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
    )


def test_effective_boundaries_none_without_scope_or_never():
    assert effective_boundaries(_agent()) is None
    assert effective_boundaries(_agent(boundary_never=[], boundary_scope=None)) is None


def test_effective_boundaries_values():
    b = effective_boundaries(
        _agent(boundary_scope="clinic", boundary_never=["advice"], boundary_check_enabled=False)
    )
    assert b == Boundaries(
        scopes=("clinic",),
        never=("advice",),
        refusal_message=DEFAULT_REFUSAL_MESSAGE,
        check_enabled=False,
    )
    b = effective_boundaries(_agent(boundary_never=["x"], boundary_refusal_message="Nope"))
    assert b is not None
    assert b.scopes == () and b.refusal_message == "Nope" and b.check_enabled is True


def test_prompt_contains_boundaries_last():
    agent = _agent(boundary_scope="clinic bookings", boundary_never=["medical advice", "prices"])
    prompt = ContextBuilder()._system_prompt(agent, _conversation(), [], "STYLE")
    rendered = render_boundaries(effective_boundaries(agent))
    assert "clinic bookings" in prompt
    assert "- medical advice" in prompt and "- prices" in prompt
    assert "ignore" in rendered and "reveal" in rendered
    assert prompt.endswith(rendered)


def test_prompt_unchanged_without_boundaries():
    agent = _agent()
    prompt = ContextBuilder()._system_prompt(agent, _conversation(), [], "STYLE")
    assert prompt == "Você é o atendente da Acme.\n\nSTYLE"
