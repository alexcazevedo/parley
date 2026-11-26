"""Agent API: sentiment/handoff fields round-trip and numeric bounds give 422."""

import os

import pytest

from app.models.agent import DEFAULT_HANDOFF_MESSAGE


async def _tenant_headers(client) -> dict:
    response = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    assert response.status_code == 201
    return {"X-API-Key": response.json()["api_key"]}


def _payload(**extra) -> dict:
    return {"name": "Bot", "instructions": "Be helpful.", "model": "gpt-4o-mini", **extra}


async def test_fields_round_trip_and_patch(client):
    headers = await _tenant_headers(client)
    created = await client.post(
        "/agents",
        json=_payload(sentiment_escalation_enabled=True, handoff_message="Calling a human."),
        headers=headers,
    )
    assert created.status_code == 201
    body = created.json()
    assert body["sentiment_escalation_enabled"] is True
    assert body["handoff_message"] == "Calling a human."
    fetched = (await client.get(f"/agents/{body['id']}", headers=headers)).json()
    assert fetched["sentiment_escalation_enabled"] is True
    assert fetched["handoff_message"] == "Calling a human."

    patched = await client.patch(
        f"/agents/{body['id']}",
        json={"sentiment_escalation_enabled": False, "handoff_message": "Other."},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["sentiment_escalation_enabled"] is False
    assert patched.json()["handoff_message"] == "Other."

    unchanged = await client.patch(
        f"/agents/{body['id']}", json={"handoff_message": None}, headers=headers
    )
    assert unchanged.status_code == 200
    assert unchanged.json()["handoff_message"] == "Other."


async def test_defaults_when_omitted(client):
    headers = await _tenant_headers(client)
    created = await client.post("/agents", json=_payload(), headers=headers)
    assert created.status_code == 201
    assert created.json()["sentiment_escalation_enabled"] is False
    assert created.json()["handoff_message"] == DEFAULT_HANDOFF_MESSAGE


BAD = [
    {"temperature": 50},
    {"temperature": -0.1},
    {"max_tokens": 0},
    {"max_tokens": 32769},
    {"max_tool_iterations": 0},
    {"max_tool_iterations": 21},
    {"context_message_limit": 0},
    {"context_message_limit": 201},
    {"summary_threshold": -1},
    {"summary_threshold": 1001},
    {"handoff_message": ""},
    {"handoff_message": "x" * 1001},
    {"name": ""},
    {"name": "x" * 256},
    {"model": ""},
    {"model": "x" * 256},
]


@pytest.mark.parametrize("fields", BAD)
async def test_out_of_range_gives_422(client, fields):
    headers = await _tenant_headers(client)
    response = await client.post("/agents", json=_payload(**fields), headers=headers)
    assert response.status_code == 422
    assert response.json()["error"]["message"]

    ok = await client.post("/agents", json=_payload(), headers=headers)
    patched = await client.patch(f"/agents/{ok.json()['id']}", json=fields, headers=headers)
    assert patched.status_code == 422
    assert patched.json()["error"]["message"]


async def test_bounds_accepted_at_edges(client):
    headers = await _tenant_headers(client)
    response = await client.post(
        "/agents",
        json=_payload(
            name="x" * 255,
            temperature=2,
            max_tokens=32768,
            max_tool_iterations=20,
            context_message_limit=200,
            summary_threshold=1000,
            handoff_message="x" * 1000,
        ),
        headers=headers,
    )
    assert response.status_code == 201
