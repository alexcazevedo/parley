"""`GET /conversations/{id}` exposes `latest_handoff` (reason, summary,
requested_by, created_at) — the warm-handoff operator brief. Newest handoff
wins regardless of resolved state; the list endpoint stays untouched (no
N+1) per `app/api/conversations.py::list_conversations`.
"""

import os
import uuid

import app.handoff.summarizer as summarizer_module
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider

BRIEF_TEXT = "Contact wants a refund; bot could not process it; user is frustrated."
OTHER_BRIEF_TEXT = "Second handoff: contact still unhappy after operator reply."


async def _setup(client, **agent_overrides) -> tuple[dict, str, str]:
    tenant_response = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant_response.json()["api_key"]}
    agent_payload = {
        "name": "Bot",
        "instructions": "Você é o atendente da Acme.",
        "model": "gpt-4o-mini",
        **agent_overrides,
    }
    agent = await client.post("/agents", json=agent_payload, headers=headers)
    assert agent.status_code == 201, agent.text
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    return headers, channel.json()["id"], agent.json()["id"]


async def _send(client, headers, channel_id, text) -> uuid.UUID:
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "user-1", "message": text},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["conversation_id"])


async def test_conversation_without_handoff_has_null_latest_handoff(
    client, fake_redis, db_session
):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "oi")

    resp = await client.get(f"/conversations/{conversation_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["latest_handoff"] is None


async def test_get_returns_populated_latest_handoff_after_handoff_with_summary(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "oi")

    summary_provider = FakeLLMProvider([LLMResponse(content=BRIEF_TEXT)])
    monkeypatch.setattr(
        summarizer_module, "get_provider", lambda agent, settings=None: summary_provider
    )

    reason = "cliente quer cancelar a assinatura"
    handoff_resp = await client.post(
        f"/conversations/{conversation_id}/handoff",
        json={"reason": reason},
        headers=headers,
    )
    assert handoff_resp.status_code == 200, handoff_resp.text

    resp = await client.get(f"/conversations/{conversation_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    latest_handoff = resp.json()["latest_handoff"]
    assert latest_handoff is not None
    assert latest_handoff["reason"] == reason
    assert latest_handoff["summary"] == BRIEF_TEXT
    assert latest_handoff["requested_by"] == "operator"
    assert latest_handoff["created_at"] is not None


async def test_get_returns_newest_of_two_handoffs(client, fake_redis, db_session, monkeypatch):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "oi")

    first_provider = FakeLLMProvider([LLMResponse(content=BRIEF_TEXT)])
    monkeypatch.setattr(
        summarizer_module, "get_provider", lambda agent, settings=None: first_provider
    )
    first_handoff = await client.post(
        f"/conversations/{conversation_id}/handoff",
        json={"reason": "primeiro motivo"},
        headers=headers,
    )
    assert first_handoff.status_code == 200, first_handoff.text

    resumed = await client.post(f"/conversations/{conversation_id}/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text

    second_provider = FakeLLMProvider([LLMResponse(content=OTHER_BRIEF_TEXT)])
    monkeypatch.setattr(
        summarizer_module, "get_provider", lambda agent, settings=None: second_provider
    )
    second_handoff = await client.post(
        f"/conversations/{conversation_id}/handoff",
        json={"reason": "segundo motivo"},
        headers=headers,
    )
    assert second_handoff.status_code == 200, second_handoff.text

    resp = await client.get(f"/conversations/{conversation_id}", headers=headers)
    assert resp.status_code == 200, resp.text
    latest_handoff = resp.json()["latest_handoff"]
    assert latest_handoff["reason"] == "segundo motivo"
    assert latest_handoff["summary"] == OTHER_BRIEF_TEXT


async def test_list_conversations_does_not_include_latest_handoff(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "oi")

    summary_provider = FakeLLMProvider([LLMResponse(content=BRIEF_TEXT)])
    monkeypatch.setattr(
        summarizer_module, "get_provider", lambda agent, settings=None: summary_provider
    )
    handoff_resp = await client.post(
        f"/conversations/{conversation_id}/handoff",
        json={"reason": "x"},
        headers=headers,
    )
    assert handoff_resp.status_code == 200, handoff_resp.text

    resp = await client.get("/conversations", headers=headers)
    assert resp.status_code == 200, resp.text
    conversations = {c["id"]: c for c in resp.json()}
    assert str(conversation_id) in conversations
    # List endpoint keeps returning conversations without the brief (avoids
    # an N+1 handoff query per row) — the field is present but always null.
    assert conversations[str(conversation_id)]["latest_handoff"] is None
