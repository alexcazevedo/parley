"""Tool name, method, timeout and description bounds; operator message length."""

import uuid

import pytest
from sqlalchemy import select

from app.config import get_settings
from app.models.message import Message
from tests.test_ingest import _auth, _create_tenant
from tests.test_operator_api import _send, _setup

ENDPOINT = "https://example.com/hook"


async def _create(client, headers, **overrides):
    body = {"name": "get_order", "endpoint": ENDPOINT, **overrides}
    return await client.post("/tools", json=body, headers=headers)


@pytest.fixture
async def headers(client):
    tenant = await _create_tenant(client)
    return _auth(tenant["api_key"])


@pytest.mark.parametrize(
    "name", ["remember", "handoff_to_human", "has space", "a.b", "a" * 65, ""]
)
async def test_create_rejects_bad_name(client, headers, name):
    response = await _create(client, headers, name=name)
    assert response.status_code == 422


async def test_reserved_name_message(client, headers):
    response = await _create(client, headers, name="remember")
    assert "reserved" in response.json()["error"]["message"] + str(
        response.json()["error"].get("details")
    )


async def test_name_boundaries_accepted(client, headers):
    assert (await _create(client, headers, name="a" * 64)).status_code == 201
    assert (await _create(client, headers, name="Ab_c-9")).status_code == 201


async def test_rename_to_builtin_rejected(client, headers):
    tool = (await _create(client, headers)).json()
    response = await client.patch(
        f"/tools/{tool['id']}", json={"name": "remember"}, headers=headers
    )
    assert response.status_code == 422
    response = await client.patch(
        f"/tools/{tool['id']}", json={"name": "bad name"}, headers=headers
    )
    assert response.status_code == 422


async def test_method_validated_and_uppercased(client, headers):
    assert (await _create(client, headers, name="t1", method="TRACE")).status_code == 422
    response = await _create(client, headers, name="t2", method="get")
    assert response.status_code == 201
    assert response.json()["method"] == "GET"
    patched = await client.patch(
        f"/tools/{response.json()['id']}", json={"method": "patch"}, headers=headers
    )
    assert patched.json()["method"] == "PATCH"
    bad = await client.patch(
        f"/tools/{response.json()['id']}", json={"method": "TRACE"}, headers=headers
    )
    assert bad.status_code == 422


@pytest.mark.parametrize("timeout", [0, 61, -1])
async def test_timeout_out_of_range(client, headers, timeout):
    assert (await _create(client, headers, timeout_seconds=timeout)).status_code == 422
    tool = (await _create(client, headers, name="ok")).json()
    response = await client.patch(
        f"/tools/{tool['id']}", json={"timeout_seconds": timeout}, headers=headers
    )
    assert response.status_code == 422


@pytest.mark.parametrize("timeout", [1, 60])
async def test_timeout_bounds_accepted(client, headers, timeout):
    assert (await _create(client, headers, timeout_seconds=timeout)).status_code == 201


async def test_description_length(client, headers):
    assert (await _create(client, headers, name="d1", description="x" * 2000)).status_code == 201
    assert (await _create(client, headers, name="d2", description="x" * 2001)).status_code == 422


async def test_operator_message_length(client, db_session):
    headers, channel_id, _ = await _setup(client)
    conversation_id = await _send(client, headers, channel_id, "user-1", "oi")
    limit = get_settings().max_message_chars
    url = f"/conversations/{conversation_id}/messages"

    too_long = await client.post(url, json={"content": "x" * (limit + 1)}, headers=headers)
    assert too_long.status_code == 422
    stored = await db_session.execute(
        select(Message).where(
            Message.conversation_id == conversation_id, Message.content == "x" * (limit + 1)
        )
    )
    assert stored.scalars().all() == []

    exact = await client.post(url, json={"content": "x" * limit}, headers=headers)
    assert exact.status_code == 201, exact.text
    assert uuid.UUID(exact.json()["id"])
