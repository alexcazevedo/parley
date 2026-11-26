"""`GET /lines`: a waiting line per category, oldest handoff first, and what
sets and clears `waiting_since` (`HandoffService`)."""

import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import update

from app.agents.runtime import AgentRuntime
from app.handoff.summarizer import handoff_with_summary
from app.llm.base import LLMResponse, ToolCall
from app.llm.providers.fake import FakeLLMProvider
from app.models.conversation import Conversation
from app.models.handoff import HandoffRequestedBy
from app.tools.builtin import BuiltinExecutor
from app.tools.registry import ToolRegistry
from tests.test_conversation_category import _category, _move
from tests.test_handoff import _drain_jobs, _run_turn
from tests.test_operator_api import _send, _setup

T0 = datetime(2026, 1, 1, tzinfo=UTC)


async def _handoff(client, headers, conversation_id):
    response = await client.post(
        f"/conversations/{conversation_id}/handoff", json={}, headers=headers
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _set_waiting(db_session, conversation_id, minutes: int) -> None:
    """Pin `waiting_since` (T0 + minutes) so order never depends on the clock."""
    await db_session.execute(
        update(Conversation)
        .where(Conversation.id == uuid.UUID(str(conversation_id)))
        .values(waiting_since=T0 + timedelta(minutes=minutes))
    )
    await db_session.commit()


async def _lines(client, headers, **params):
    response = await client.get("/lines", params=params, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


def _ids(line) -> list[str]:
    return [c["id"] for c in line["conversations"]]


async def _waiting_since(db_session, conversation_id):
    conversation = await db_session.get(Conversation, uuid.UUID(str(conversation_id)))
    await db_session.refresh(conversation)
    return conversation.waiting_since


async def test_lines_group_by_category_oldest_first(client, db_session, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    billing = await _category(client, headers, "billing")
    support = await _category(client, headers, "support")
    b_new = await _send(client, headers, channel_id, "u1", "oi")
    b_old = await _send(client, headers, channel_id, "u2", "oi")
    s1 = await _send(client, headers, channel_id, "u3", "oi")
    for conv, cat in ((b_new, billing), (b_old, billing), (s1, support)):
        assert (await _move(client, headers, conv, cat)).status_code == 200
        await _handoff(client, headers, conv)
    await _set_waiting(db_session, b_new, 10)
    await _set_waiting(db_session, b_old, 1)
    await _set_waiting(db_session, s1, 5)

    lines = await _lines(client, headers)
    assert [(line["category_id"], line["category_name"]) for line in lines] == [
        (billing, "billing"),
        (support, "support"),
        (None, None),
    ]
    assert _ids(lines[0]) == [str(b_old), str(b_new)]
    assert _ids(lines[1]) == [str(s1)]
    assert lines[2]["conversations"] == []
    first = lines[0]["conversations"][0]
    assert first["contact_external_user_id"] == "u2"
    assert first["waiting_since"] is not None
    assert first["claimed_by"] is None and first["latest_handoff"] is None


async def test_uncategorized_line_and_empty_category(client, db_session, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    await _category(client, headers, "billing")
    conv = await _send(client, headers, channel_id, "u1", "oi")
    await _handoff(client, headers, conv)

    lines = await _lines(client, headers)
    assert lines[0]["category_name"] == "billing" and lines[0]["conversations"] == []
    assert lines[-1]["category_id"] is None
    assert _ids(lines[-1]) == [str(conv)]


async def test_no_categories_still_has_the_uncategorized_line(client):
    headers, _, _ = await _setup(client)
    lines = await _lines(client, headers)
    assert lines == [{"category_id": None, "category_name": None, "conversations": []}]


async def test_only_unclaimed_human_handoff_conversations(client, db_session, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    active = await _send(client, headers, channel_id, "u1", "oi")
    claimed = await _send(client, headers, channel_id, "u2", "oi")
    waiting = await _send(client, headers, channel_id, "u3", "oi")
    await _handoff(client, headers, claimed)
    await _handoff(client, headers, waiting)
    await db_session.execute(
        update(Conversation)
        .where(Conversation.id == claimed)
        .values(claimed_by="op-1", claimed_at=T0)
    )
    await db_session.commit()

    lines = await _lines(client, headers)
    assert _ids(lines[-1]) == [str(waiting)]
    assert str(active) not in _ids(lines[-1])


async def test_move_keeps_position_and_waiting_since(client, db_session, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    billing = await _category(client, headers, "billing")
    support = await _category(client, headers, "support")
    older = await _send(client, headers, channel_id, "u1", "oi")
    later = await _send(client, headers, channel_id, "u2", "oi")
    assert (await _move(client, headers, older, billing)).status_code == 200
    assert (await _move(client, headers, later, support)).status_code == 200
    await _handoff(client, headers, older)
    await _handoff(client, headers, later)
    await _set_waiting(db_session, older, 1)
    await _set_waiting(db_session, later, 9)

    moved = await _move(client, headers, older, support)
    assert moved.status_code == 200
    assert moved.json()["waiting_since"] == (T0 + timedelta(minutes=1)).isoformat().replace(
        "+00:00", "Z"
    )
    lines = await _lines(client, headers)
    assert _ids(lines[0]) == []
    assert _ids(lines[1]) == [str(older), str(later)]
    assert await _waiting_since(db_session, older) == T0 + timedelta(minutes=1)


async def test_resume_and_close_clear_and_a_second_handoff_is_later(
    client, db_session, fake_handoff_brief
):
    headers, channel_id, _ = await _setup(client)
    resumed = await _send(client, headers, channel_id, "u1", "oi")
    closed = await _send(client, headers, channel_id, "u2", "oi")
    await _handoff(client, headers, resumed)
    await _handoff(client, headers, closed)
    await _set_waiting(db_session, resumed, 0)
    await db_session.execute(
        update(Conversation)
        .where(Conversation.id == closed)
        .values(claimed_by="op-1", claimed_at=T0)
    )
    await db_session.commit()
    first = T0

    for conv, action in ((resumed, "resume"), (closed, "close")):
        out = await client.post(f"/conversations/{conv}/{action}", headers=headers)
        assert out.status_code == 200, out.text
        assert out.json()["waiting_since"] is None
        assert out.json()["claimed_by"] is None and out.json()["claimed_at"] is None
    assert _ids((await _lines(client, headers))[-1]) == []
    assert await _waiting_since(db_session, closed) is None

    again = await _handoff(client, headers, resumed)
    assert again["waiting_since"] is not None
    assert (await _waiting_since(db_session, resumed)) > first
    assert _ids((await _lines(client, headers))[-1]) == [str(resumed)]


async def test_operator_endpoint_sets_waiting_since(client, db_session, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    conv = await _send(client, headers, channel_id, "u1", "oi")
    out = await _handoff(client, headers, conv)
    assert out["state"] == "HUMAN_HANDOFF"
    assert out["waiting_since"] is not None
    assert out["claimed_by"] is None


async def test_agent_tool_handoff_sets_waiting_since(
    client, fake_redis, db_session, fake_handoff_brief
):
    headers, channel_id, _ = await _setup(client)
    conv = await _send(client, headers, channel_id, "u1", "quero uma pessoa")
    await _drain_jobs(fake_redis)
    provider = FakeLLMProvider(
        [
            LLMResponse(
                tool_calls=[
                    ToolCall(id="c1", name="handoff_to_human", arguments={"reason": "pediu"})
                ]
            )
        ]
    )
    await _run_turn(
        fake_redis,
        db_session,
        AgentRuntime(provider=provider, registry=ToolRegistry([BuiltinExecutor()])),
    )
    assert await _waiting_since(db_session, conv) is not None
    assert _ids((await _lines(client, headers))[-1]) == [str(conv)]


async def test_handoff_with_summary_sets_waiting_since(
    client, db_session, fake_handoff_brief
):
    """Sentiment handoffs go through `handoff_with_summary` (turn.py), as the tool does."""
    headers, channel_id, _ = await _setup(client)
    conv_id = await _send(client, headers, channel_id, "u1", "oi")
    conversation = await db_session.get(Conversation, conv_id)
    await handoff_with_summary(db_session, conversation, "frustrated", HandoffRequestedBy.AGENT)
    await db_session.commit()
    assert await _waiting_since(db_session, conv_id) is not None


async def test_limit_caps_the_total_keeping_the_oldest(client, db_session, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    billing = await _category(client, headers, "billing")
    convs = []
    for i in range(3):
        conv = await _send(client, headers, channel_id, f"u{i}", "oi")
        convs.append(conv)
        await _handoff(client, headers, conv)
    assert (await _move(client, headers, convs[0], billing)).status_code == 200
    for i, conv in enumerate(convs):
        await _set_waiting(db_session, conv, 10 - i)  # convs[2] oldest

    lines = await _lines(client, headers, limit=2)
    assert _ids(lines[0]) == []  # the billing one is the newest, so it is cut
    assert _ids(lines[1]) == [str(convs[2]), str(convs[1])]
    assert (await client.get("/lines", params={"limit": 0}, headers=headers)).status_code == 422
    assert (await client.get("/lines", params={"limit": 501}, headers=headers)).status_code == 422


async def test_include_handoff_attaches_the_brief(client, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    conv = await _send(client, headers, channel_id, "u1", "oi")
    await _handoff(client, headers, conv)
    plain = (await _lines(client, headers))[-1]["conversations"][0]
    assert plain["latest_handoff"] is None
    full = (await _lines(client, headers, include_handoff="true"))[-1]["conversations"][0]
    assert full["latest_handoff"]["summary"] == fake_handoff_brief


async def test_other_tenants_never_appear(client, db_session, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    mine = await _category(client, headers, "billing")
    conv = await _send(client, headers, channel_id, "u1", "oi")
    await _handoff(client, headers, conv)
    other = await client.post(
        "/tenants", json={"name": "Other"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    other_headers = {"X-API-Key": other.json()["api_key"]}
    lines = await _lines(client, other_headers)
    assert lines == [{"category_id": None, "category_name": None, "conversations": []}]
    assert mine not in str(lines)
