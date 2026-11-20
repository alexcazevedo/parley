"""`GET /conversations` as the operator inbox/Ativas data source: repeatable
`state` filter, newest-activity-first ordering, `limit`, the contact label
attached to every row, and the opt-in `include_handoff` brief per row (one
bounded statement, no N+1) — see `app/api/conversations.py::list_conversations`.
"""

import os
import uuid

from sqlalchemy import event

import app.handoff.summarizer as summarizer_module
from app.llm.base import LLMResponse
from app.llm.providers.fake import FakeLLMProvider
from app.models.conversation import Conversation, ConversationState
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.workers.main import run_once

DEBOUNCE_SECONDS = 0.2
BRIEF_TEXT = "Contact wants a refund; still frustrated."
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


async def _send(client, headers, channel_id, user_id, text) -> uuid.UUID:
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": user_id, "message": text},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["conversation_id"])


async def _drain_jobs(fake_redis) -> None:
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None


async def test_multi_state_filter_and_single_state_still_works(
    client, fake_redis, db_session, fake_handoff_brief
):
    headers, channel_id, _ = await _setup(client)

    active_id = await _send(client, headers, channel_id, "user-1", "oi")
    await _drain_jobs(fake_redis)

    waiting_id = await _send(client, headers, channel_id, "user-2", "oi")
    await _drain_jobs(fake_redis)
    waiting_conv = await db_session.get(Conversation, waiting_id)
    waiting_conv.state = ConversationState.WAITING
    await db_session.commit()

    handed_off_id = await _send(client, headers, channel_id, "user-3", "quero um humano")
    await _drain_jobs(fake_redis)
    handoff_resp = await client.post(
        f"/conversations/{handed_off_id}/handoff", json={"reason": "x"}, headers=headers
    )
    assert handoff_resp.status_code == 200, handoff_resp.text

    closed_id = await _send(client, headers, channel_id, "user-4", "tchau")
    await _drain_jobs(fake_redis)
    close_resp = await client.post(f"/conversations/{closed_id}/close", headers=headers)
    assert close_resp.status_code == 200, close_resp.text

    resp = await client.get(
        "/conversations",
        params=[("state", "ACTIVE_AI"), ("state", "WAITING")],
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    ids = {c["id"] for c in resp.json()}
    assert ids == {str(active_id), str(waiting_id)}

    single_state_resp = await client.get(
        "/conversations", params={"state": "HUMAN_HANDOFF"}, headers=headers
    )
    assert single_state_resp.status_code == 200
    assert {c["id"] for c in single_state_resp.json()} == {str(handed_off_id)}

    no_filter_resp = await client.get("/conversations", headers=headers)
    assert {c["id"] for c in no_filter_resp.json()} == {
        str(active_id),
        str(waiting_id),
        str(handed_off_id),
        str(closed_id),
    }


async def test_ordering_newest_activity_first_nulls_last(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client)

    # Created first, will remain the oldest of the null-`last_message_at`
    # conversations, and gets `last_message_at` set to `None` directly on the
    # row (redundant with the ingest default, but pins the "never touched"
    # case explicitly per the ticket).
    oldest_id = await _send(client, headers, channel_id, "user-1", "oi")
    await _drain_jobs(fake_redis)
    oldest_conv = await db_session.get(Conversation, oldest_id)
    oldest_conv.last_message_at = None
    await db_session.commit()

    middle_id = await _send(client, headers, channel_id, "user-2", "oi")
    await _drain_jobs(fake_redis)

    bumped_id = await _send(client, headers, channel_id, "user-3", "oi")
    await _drain_jobs(fake_redis)
    # Bump this conversation's activity by sending a later message.
    bump_resp = await client.post(
        f"/conversations/{bumped_id}/messages",
        json={"content": "ainda aqui?"},
        headers=headers,
    )
    assert bump_resp.status_code == 201, bump_resp.text

    resp = await client.get("/conversations", headers=headers)
    assert resp.status_code == 200, resp.text
    ids = [c["id"] for c in resp.json()]
    assert ids[0] == str(bumped_id)
    assert ids[-1] == str(oldest_id)
    assert str(middle_id) in ids


async def test_limit_bounds_the_page(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client)
    for i in range(3):
        await _send(client, headers, channel_id, f"user-{i}", "oi")
        await _drain_jobs(fake_redis)

    resp = await client.get("/conversations", params={"limit": 2}, headers=headers)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 2


async def test_include_handoff_populates_briefs_without_n_plus_1(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id, _ = await _setup(client)

    # Never handed off.
    untouched_id = await _send(client, headers, channel_id, "user-1", "oi")
    await _drain_jobs(fake_redis)

    # Two handoffs on the same conversation — the newest brief must win.
    twice_id = await _send(client, headers, channel_id, "user-2", "quero um humano")
    await _drain_jobs(fake_redis)
    first_provider = FakeLLMProvider([LLMResponse(content=BRIEF_TEXT)])
    monkeypatch.setattr(
        summarizer_module, "get_provider", lambda agent, settings=None: first_provider
    )
    first_handoff = await client.post(
        f"/conversations/{twice_id}/handoff", json={"reason": "primeiro"}, headers=headers
    )
    assert first_handoff.status_code == 200, first_handoff.text
    resumed = await client.post(f"/conversations/{twice_id}/resume", headers=headers)
    assert resumed.status_code == 200, resumed.text
    second_provider = FakeLLMProvider([LLMResponse(content=OTHER_BRIEF_TEXT)])
    monkeypatch.setattr(
        summarizer_module, "get_provider", lambda agent, settings=None: second_provider
    )
    second_handoff = await client.post(
        f"/conversations/{twice_id}/handoff", json={"reason": "segundo"}, headers=headers
    )
    assert second_handoff.status_code == 200, second_handoff.text

    # Single handoff on a third conversation.
    once_id = await _send(client, headers, channel_id, "user-3", "quero um humano tambem")
    await _drain_jobs(fake_redis)
    third_provider = FakeLLMProvider([LLMResponse(content=BRIEF_TEXT)])
    monkeypatch.setattr(
        summarizer_module, "get_provider", lambda agent, settings=None: third_provider
    )
    third_handoff = await client.post(
        f"/conversations/{once_id}/handoff", json={"reason": "terceiro"}, headers=headers
    )
    assert third_handoff.status_code == 200, third_handoff.text

    statements: list[str] = []

    def _count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    engine = db_session.bind
    event.listen(engine.sync_engine, "before_cursor_execute", _count)
    try:
        resp = await client.get(
            "/conversations", params={"include_handoff": "true"}, headers=headers
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _count)

    assert resp.status_code == 200, resp.text
    by_id = {c["id"]: c for c in resp.json()}

    assert by_id[str(untouched_id)]["latest_handoff"] is None
    assert by_id[str(once_id)]["latest_handoff"]["reason"] == "terceiro"
    assert by_id[str(once_id)]["latest_handoff"]["summary"] == BRIEF_TEXT
    assert by_id[str(twice_id)]["latest_handoff"]["reason"] == "segundo"
    assert by_id[str(twice_id)]["latest_handoff"]["summary"] == OTHER_BRIEF_TEXT

    # Bounded number of statements for the whole request regardless of how
    # many conversations/handoffs exist — no per-row handoff query.
    assert len(statements) <= 6, statements


async def test_include_handoff_omitted_keeps_latest_handoff_null(
    client, fake_redis, db_session, monkeypatch
):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "user-1", "quero um humano")
    await _drain_jobs(fake_redis)

    summary_provider = FakeLLMProvider([LLMResponse(content=BRIEF_TEXT)])
    monkeypatch.setattr(
        summarizer_module, "get_provider", lambda agent, settings=None: summary_provider
    )
    handoff_resp = await client.post(
        f"/conversations/{conversation_id}/handoff", json={"reason": "x"}, headers=headers
    )
    assert handoff_resp.status_code == 200, handoff_resp.text

    resp = await client.get("/conversations", headers=headers)
    assert resp.status_code == 200, resp.text
    for row in resp.json():
        assert row["latest_handoff"] is None


async def test_contact_label_present_on_list_and_single_get(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "user-42", "oi")
    await _drain_jobs(fake_redis)

    list_resp = await client.get("/conversations", headers=headers)
    assert list_resp.status_code == 200, list_resp.text
    row = next(c for c in list_resp.json() if c["id"] == str(conversation_id))
    assert row["contact_external_user_id"] == "user-42"
    assert "contact_display_name" in row

    get_resp = await client.get(f"/conversations/{conversation_id}", headers=headers)
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["contact_external_user_id"] == "user-42"
    assert "contact_display_name" in get_resp.json()


async def test_tenant_isolation(client, fake_redis, db_session):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "user-1", "oi")
    await _drain_jobs(fake_redis)

    other_headers, other_channel_id, _ = await _setup(client)
    other_conversation_id = await _send(client, other_headers, other_channel_id, "user-1", "oi")
    await _drain_jobs(fake_redis)

    resp = await client.get("/conversations", headers=headers)
    assert resp.status_code == 200, resp.text
    ids = {c["id"] for c in resp.json()}
    assert str(conversation_id) in ids
    assert str(other_conversation_id) not in ids
