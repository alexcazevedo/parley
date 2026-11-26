"""Claim and release: one conditional UPDATE each, so two operators never both win."""

import asyncio
import os
import uuid

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.conversation import Conversation
from app.models.tenant import Tenant
from app.repositories.conversations import ConversationsRepository
from tests.test_operator_api import _send, _setup
from tests.test_waiting_lines import _handoff, _ids, _lines, _set_waiting


async def _claim(client, headers, conv, operator="ana"):
    return await client.post(
        f"/conversations/{conv}/claim", json={"operator_id": operator}, headers=headers
    )


async def _release(client, headers, conv):
    return await client.post(f"/conversations/{conv}/release", headers=headers)


async def _waiting(client, headers, channel_id, user="u1"):
    conv = await _send(client, headers, channel_id, user, "oi")
    await _handoff(client, headers, conv)
    return conv


async def test_claim_removes_from_lines_and_filters_by_claimed_by(client, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    conv = await _waiting(client, headers, channel_id)
    response = await _claim(client, headers, conv)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["claimed_by"] == "ana" and body["claimed_at"] is not None
    assert body["contact_external_user_id"] == "u1"
    assert _ids((await _lines(client, headers))[-1]) == []
    mine = await client.get("/conversations", params={"claimed_by": "ana"}, headers=headers)
    assert [c["id"] for c in mine.json()] == [str(conv)]
    other = await client.get("/conversations", params={"claimed_by": "bia"}, headers=headers)
    assert other.json() == []


async def test_second_operator_gets_409_and_first_keeps_it(client, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    conv = await _waiting(client, headers, channel_id)
    await _claim(client, headers, conv, "ana")
    lost = await _claim(client, headers, conv, "bia")
    assert lost.status_code == 409
    assert lost.json()["error"]["message"] == "conversation is already claimed"
    got = (await client.get(f"/conversations/{conv}", headers=headers)).json()
    assert got["claimed_by"] == "ana"


async def test_same_operator_again_keeps_claimed_at(client, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    conv = await _waiting(client, headers, channel_id)
    first = (await _claim(client, headers, conv)).json()
    again = await _claim(client, headers, conv)
    assert again.status_code == 200
    assert again.json()["claimed_at"] == first["claimed_at"]


async def test_release_returns_to_place_and_anyone_can_claim(
    client, db_session, fake_handoff_brief
):
    headers, channel_id, _ = await _setup(client)
    a = await _waiting(client, headers, channel_id, "u1")
    b = await _waiting(client, headers, channel_id, "u2")
    c = await _waiting(client, headers, channel_id, "u3")
    for conv, minutes in ((a, 1), (b, 2), (c, 3)):
        await _set_waiting(db_session, conv, minutes)
    assert (await _claim(client, headers, b)).status_code == 200
    assert _ids((await _lines(client, headers))[-1]) == [str(a), str(c)]

    released = await _release(client, headers, b)
    assert released.status_code == 200, released.text
    body = released.json()
    assert body["claimed_by"] is None and body["claimed_at"] is None
    assert body["waiting_since"] is not None
    assert _ids((await _lines(client, headers))[-1]) == [str(a), str(b), str(c)]
    assert (await _claim(client, headers, b, "bia")).json()["claimed_by"] == "bia"


async def test_release_unclaimed_is_409(client, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    conv = await _waiting(client, headers, channel_id)
    response = await _release(client, headers, conv)
    assert response.status_code == 409
    assert response.json()["error"]["message"] == "conversation is not claimed"


async def test_claim_not_waiting_is_409(client, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    active = await _send(client, headers, channel_id, "u1", "oi")
    closed = await _waiting(client, headers, channel_id, "u2")
    assert (await client.post(f"/conversations/{closed}/close", headers=headers)).status_code == 200
    for conv in (active, closed):
        response = await _claim(client, headers, conv)
        assert response.status_code == 409
        assert response.json()["error"]["message"] == "conversation is not waiting for an operator"


async def test_resume_and_close_leave_claim_null(client, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    for action in ("resume", "close"):
        conv = await _waiting(client, headers, channel_id, f"u-{action}")
        await _claim(client, headers, conv)
        out = await client.post(f"/conversations/{conv}/{action}", headers=headers)
        assert out.json()["claimed_by"] is None and out.json()["claimed_at"] is None


async def test_operator_id_is_stripped_and_bounded(client, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    conv = await _waiting(client, headers, channel_id)
    for bad in ("", "   ", "x" * 201):
        assert (await _claim(client, headers, conv, bad)).status_code == 422
    empty = await client.post(f"/conversations/{conv}/claim", json={}, headers=headers)
    assert empty.status_code == 422
    ok = await _claim(client, headers, conv, "  ana  ")
    assert ok.status_code == 200 and ok.json()["claimed_by"] == "ana"


async def test_unknown_and_other_tenant_are_404(client, fake_handoff_brief):
    headers, channel_id, _ = await _setup(client)
    conv = await _waiting(client, headers, channel_id)
    other = await client.post(
        "/tenants", json={"name": "Other"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    other_headers = {"X-API-Key": other.json()["api_key"]}
    await _claim(client, headers, conv)
    assert (await _claim(client, other_headers, conv)).status_code == 404
    assert (await _release(client, other_headers, conv)).status_code == 404
    unknown = uuid.uuid4()
    assert (await _claim(client, headers, unknown)).status_code == 404
    assert (await _release(client, headers, unknown)).status_code == 404
    still = (await client.get(f"/conversations/{conv}", headers=headers)).json()
    assert still["claimed_by"] == "ana"


async def test_two_simultaneous_claims_one_wins(
    client, db_session, test_engine, fake_handoff_brief
):
    headers, channel_id, _ = await _setup(client)
    conv = await _waiting(client, headers, channel_id)
    row = await db_session.get(Conversation, conv)
    tenant_id = row.tenant_id
    await db_session.rollback()
    maker = async_sessionmaker(bind=test_engine, expire_on_commit=False)

    async def attempt(operator: str):
        async with maker() as session:
            won = await ConversationsRepository(session).claim(tenant_id, conv, operator)
            await session.commit()
            return won

    try:
        results = await asyncio.gather(attempt("ana"), attempt("bia"))
        winners = [r for r in results if r is not None]
        assert len(winners) == 1
        assert winners[0].claimed_by in ("ana", "bia")
    finally:
        async with maker() as session:
            await session.execute(delete(Tenant).where(Tenant.id == tenant_id))
            await session.commit()
