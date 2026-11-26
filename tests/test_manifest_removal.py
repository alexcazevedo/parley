"""Manifest removal and adoption: an item removed from the file is deleted
only when the manifest manages it and nothing would be lost; a hand-made
tenant is taken over by name (or key) without duplicates."""

import copy
import os
import uuid
from contextlib import asynccontextmanager

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.manifest.apply import apply_manifest, parse_manifest
from app.models.agent import Agent
from app.models.category import Category
from app.models.channel import Channel
from app.models.contact import Contact
from app.models.conversation import Conversation
from app.models.manifest_item import ManifestItem
from app.models.message import Message
from app.models.tenant import Tenant
from app.models.tool import Tool

TOKEN = "s3cr3t-removal-token"
ORDERS_URL = "https://orders.example.com/orders"
EVENTS_URL = "https://inbox-removal.example.com/events"

AGENT = {
    "key": "main",
    "name": "Loja",
    "instructions": "Você é o atendente da Loja.",
    "model": "gpt-4o-mini",
    "tool_names": ["get_order"],
}
TOOL = {
    "name": "get_order",
    "description": "Look up an order",
    "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}},
    "endpoint": ORDERS_URL,
    "method": "GET",
    "auth_config": {"type": "bearer", "token": {"secret": "orders_token"}},
}
MANIFEST = {
    "version": 1,
    "agents": [AGENT],
    "departments": [
        {"name": "billing", "description": "Pagamentos"},
        {"name": "shipping", "description": "Entregas"},
    ],
    "tools": [TOOL],
    "channels": [
        {"key": "site", "type": "webchat", "name": "Site", "agent": "main"},
        {
            "key": "inbox",
            "type": "external",
            "name": "Inbox",
            "agent": "main",
            "config": {"events_url": EVENTS_URL},
        },
    ],
}


def _manifest() -> dict:
    return copy.deepcopy(MANIFEST)


async def _tenant(client) -> tuple[dict, uuid.UUID]:
    response = await client.post(
        "/tenants", json={"name": "Loja"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    body = response.json()
    headers = {"X-API-Key": body["api_key"]}
    stored = await client.put("/secrets/orders_token", json={"value": TOKEN}, headers=headers)
    assert stored.status_code == 204
    return headers, uuid.UUID(body["id"])


async def _apply(client, headers, data, *, dry_run=False):
    response = await client.post(
        f"/manifest/apply?dry_run={'true' if dry_run else 'false'}", json=data, headers=headers
    )
    assert TOKEN not in response.text
    return response


def _actions(body) -> list[tuple[str, str, str, list[str]]]:
    return [(c["kind"], c["key"], c["action"], c["fields"]) for c in body["changes"]]


def _details(response) -> list[dict[str, str]]:
    assert response.status_code == 422, response.text
    return response.json()["error"]["details"]


async def _count(db_session, model, tenant_id) -> int:
    return await db_session.scalar(
        select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
    )


async def _item_id(db_session, tenant_id, kind, key) -> uuid.UUID | None:
    return await db_session.scalar(
        select(ManifestItem.item_id).where(
            ManifestItem.tenant_id == tenant_id,
            ManifestItem.kind == kind,
            ManifestItem.key == key,
        )
    )


async def _conversation(client, headers, channel_id, user="u1") -> str:
    sent = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": str(channel_id), "user_id": user, "message": "oi"},
        headers=headers,
    )
    assert sent.status_code == 201, sent.text
    return sent.json()["conversation_id"]


@asynccontextmanager
async def _writes(test_engine):
    seen: list[str] = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith(("INSERT", "UPDATE", "DELETE")):
            seen.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", listener)
    try:
        yield seen
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", listener)


async def _hand_made(client, headers) -> dict[str, str]:
    """A tenant set up by hand through the API, the way the manifest would."""
    tool = {**TOOL, "auth_config": {"type": "bearer", "token": TOKEN}}
    ids = {"tool": (await client.post("/tools", json=tool, headers=headers)).json()["id"]}
    department = await client.post("/categories", json={"name": "billing"}, headers=headers)
    ids["department"] = department.json()["id"]
    agent = {k: v for k, v in AGENT.items() if k != "key"}
    agent["instructions"] = "Persona escrita à mão."
    ids["agent"] = (await client.post("/agents", json=agent, headers=headers)).json()["id"]
    site = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Site", "agent_id": ids["agent"]},
        headers=headers,
    )
    ids["site"] = site.json()["id"]
    inbox = await client.post(
        "/channels",
        json={
            "type": "external",
            "name": "Inbox",
            "agent_id": ids["agent"],
            "config": {"events_url": EVENTS_URL},
        },
        headers=headers,
    )
    assert inbox.status_code == 201, inbox.text
    ids["inbox"] = inbox.json()["id"]
    return ids


async def test_removed_items_are_deleted_in_order_and_the_dry_run_shows_it(
    client, db_session, test_engine
):
    headers, tenant_id = await _tenant(client)
    assert (await _apply(client, headers, _manifest())).status_code == 200
    hand = await client.post("/categories", json={"name": "hand"}, headers=headers)
    assert hand.status_code == 201

    empty = {"version": 1}
    expected = [
        ("channel", "inbox", "delete", []),
        ("channel", "site", "delete", []),
        ("agent", "main", "delete", []),
        ("tool", "get_order", "delete", []),
        ("department", "billing", "delete", []),
        ("department", "shipping", "delete", []),
    ]
    async with _writes(test_engine) as writes:
        dry = await _apply(client, headers, empty, dry_run=True)
    assert dry.status_code == 200, dry.text
    assert _actions(dry.json()) == expected
    assert writes == []
    assert await _count(db_session, ManifestItem, tenant_id) == 6

    response = await _apply(client, headers, empty)
    assert response.status_code == 200, response.text
    assert _actions(response.json()) == expected
    for model in (Channel, Agent, Tool, ManifestItem):
        assert await _count(db_session, model, tenant_id) == 0
    names = await db_session.scalars(select(Category.name).where(Category.tenant_id == tenant_id))
    assert list(names) == ["hand"]


async def test_removing_one_department_deletes_only_it(client, db_session):
    headers, tenant_id = await _tenant(client)
    await _apply(client, headers, _manifest())
    data = _manifest()
    data["departments"] = data["departments"][:1]
    response = await _apply(client, headers, data)
    assert response.status_code == 200, response.text
    changed = [c for c in _actions(response.json()) if c[2] != "unchanged"]
    assert changed == [("department", "shipping", "delete", [])]
    assert await _count(db_session, Category, tenant_id) == 1
    assert await _item_id(db_session, tenant_id, "department", "shipping") is None
    again = await _apply(client, headers, data)
    assert {c["action"] for c in again.json()["changes"]} == {"unchanged"}


async def test_a_department_is_deleted_once_its_conversations_are_closed(client, db_session):
    headers, tenant_id = await _tenant(client)
    await _apply(client, headers, _manifest())
    site = await _item_id(db_session, tenant_id, "channel", "site")
    billing = await _item_id(db_session, tenant_id, "department", "billing")
    conversation_id = await _conversation(client, headers, site)
    moved = await client.put(
        f"/conversations/{conversation_id}/category",
        json={"category_id": str(billing)},
        headers=headers,
    )
    assert moved.status_code == 200, moved.text

    data = _manifest()
    data["departments"] = data["departments"][1:]
    assert _details(await _apply(client, headers, data, dry_run=True)) == [
        {
            "field": "departments",
            "message": "department 'billing' has 1 open conversation; "
            "close them or move them to another department first",
        }
    ]
    assert _details(await _apply(client, headers, data))[0]["field"] == "departments"
    assert await _item_id(db_session, tenant_id, "department", "billing") == billing

    closed = await client.post(f"/conversations/{conversation_id}/close", headers=headers)
    assert closed.status_code == 200, closed.text
    messages_before = await _count(db_session, Message, tenant_id)
    assert messages_before >= 1
    response = await _apply(client, headers, data)
    assert response.status_code == 200, response.text
    assert ("department", "billing", "delete", []) in _actions(response.json())
    assert await db_session.get(Category, billing) is None
    conversation = await db_session.get(Conversation, uuid.UUID(conversation_id))
    await db_session.refresh(conversation)
    assert conversation.category_id is None
    assert await _count(db_session, Message, tenant_id) == messages_before


async def test_a_channel_with_a_conversation_is_refused_and_nothing_is_written(
    client, db_session, test_engine
):
    headers, tenant_id = await _tenant(client)
    await _apply(client, headers, _manifest())
    site = await _item_id(db_session, tenant_id, "channel", "site")
    await _conversation(client, headers, site)

    data = _manifest()
    data["channels"] = data["channels"][1:]  # site removed
    data["agents"][0]["instructions"] = "Nova persona."  # an unrelated change
    data["departments"].append({"name": "returns"})
    for dry_run in (True, False):
        async with _writes(test_engine) as writes:
            response = await _apply(client, headers, data, dry_run=dry_run)
        assert _details(response) == [
            {
                "field": "channels",
                "message": "channel 'site' has 1 conversation; deleting it would erase it",
            }
        ]
        assert writes == []
    assert await db_session.get(Channel, site) is not None
    assert await _count(db_session, Category, tenant_id) == 2
    agent = await db_session.get(Agent, await _item_id(db_session, tenant_id, "agent", "main"))
    await db_session.refresh(agent)
    assert agent.instructions == AGENT["instructions"]

    # The agent has the same conversation: removing it is refused too.
    response = await _apply(client, headers, {"version": 1})
    assert _details(response) == [
        {
            "field": "channels",
            "message": "channel 'site' has 1 conversation; deleting it would erase it",
        },
        {
            "field": "agents",
            "message": "agent 'main' has 1 conversation; deleting it would erase it",
        },
    ]
    assert await _count(db_session, ManifestItem, tenant_id) == 6


async def test_an_item_still_in_use_by_a_hand_made_one_is_refused(client, db_session):
    headers, tenant_id = await _tenant(client)
    await _apply(client, headers, _manifest())
    main = await _item_id(db_session, tenant_id, "agent", "main")
    by_hand = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Hand web", "agent_id": str(main)},
        headers=headers,
    )
    assert by_hand.status_code == 201
    hand_agent = {k: v for k, v in AGENT.items() if k != "key"}
    created = await client.post("/agents", json={**hand_agent, "name": "Hand"}, headers=headers)
    assert created.status_code == 201

    response = await _apply(client, headers, {"version": 1, "departments": MANIFEST["departments"]})
    assert _details(response) == [
        {
            "field": "agents",
            "message": "agent 'main' is the agent of channel 'Hand web'; "
            "delete the channel or bind it to another agent first",
        },
        {
            "field": "tools",
            "message": "tool 'get_order' is used by agent 'Hand'; remove it from tool_names first",
        },
    ]
    # A tool still named by an agent of the file is refused as well.
    data = _manifest()
    data["tools"] = []
    assert _details(await _apply(client, headers, data)) == [
        {
            "field": "tools",
            "message": "tool 'get_order' is used by agent 'main'; remove it from tool_names first",
        },
        {
            "field": "tools",
            "message": "tool 'get_order' is used by agent 'Hand'; remove it from tool_names first",
        },
    ]
    assert await _count(db_session, ManifestItem, tenant_id) == 6


async def test_hand_made_items_survive_every_apply(client, db_session, test_engine):
    headers, tenant_id = await _tenant(client)
    ids = await _hand_made(client, headers)
    await client.post("/categories", json={"name": "other"}, headers=headers)
    counts = {m: await _count(db_session, m, tenant_id) for m in (Agent, Category, Tool, Channel)}

    async with _writes(test_engine) as writes:
        response = await _apply(client, headers, {"version": 1})
    assert response.status_code == 200, response.text
    assert response.json()["changes"] == []
    assert writes == []

    # Adopt, then remove from the file: the unadopted ones are never touched.
    data = _manifest()
    data["channels"] = []
    data["departments"] = [{"name": "billing"}]
    await _apply(client, headers, data)
    await _apply(client, headers, {"version": 1, "tools": [TOOL], "agents": [AGENT]})
    assert await db_session.get(Category, uuid.UUID(ids["department"])) is None
    for model in (Agent, Tool, Channel):
        assert await _count(db_session, model, tenant_id) == counts[model]
    names = await db_session.scalars(select(Category.name).where(Category.tenant_id == tenant_id))
    assert list(names) == ["other"]


async def test_a_hand_made_tenant_is_adopted_without_duplicates(client, db_session, test_engine):
    headers, tenant_id = await _tenant(client)
    ids = await _hand_made(client, headers)
    inbox = await db_session.get(Channel, uuid.UUID(ids["inbox"]))
    inbox_secret = inbox.secret_encrypted
    data = _manifest()
    data["departments"] = data["departments"][:1]
    expected = [
        ("department", "billing", "adopt", ["description"]),
        ("tool", "get_order", "adopt", []),
        ("agent", "main", "adopt", ["instructions"]),
        ("channel", "site", "adopt", []),
        ("channel", "inbox", "adopt", []),
    ]

    async with _writes(test_engine) as writes:
        dry = await _apply(client, headers, data, dry_run=True)
    assert dry.status_code == 200, dry.text
    assert _actions(dry.json()) == expected
    assert writes == []
    assert await _count(db_session, ManifestItem, tenant_id) == 0

    response = await _apply(client, headers, data)
    assert response.status_code == 200, response.text
    assert _actions(response.json()) == expected
    assert response.json()["generated"] == {}
    for kind, key, name in [
        ("department", "billing", "department"),
        ("tool", "get_order", "tool"),
        ("agent", "main", "agent"),
        ("channel", "site", "site"),
        ("channel", "inbox", "inbox"),
    ]:
        assert str(await _item_id(db_session, tenant_id, kind, key)) == ids[name]
    for model, count in ((Agent, 1), (Category, 1), (Tool, 1), (Channel, 2)):
        assert await _count(db_session, model, tenant_id) == count
    agent = await db_session.get(Agent, uuid.UUID(ids["agent"]))
    await db_session.refresh(agent)
    assert agent.instructions == AGENT["instructions"]
    await db_session.refresh(inbox)
    assert inbox.secret_encrypted == inbox_secret

    again = await _apply(client, headers, data)
    assert {c["action"] for c in again.json()["changes"]} == {"unchanged"}


async def test_several_matches_are_refused(client, db_session):
    headers, tenant_id = await _tenant(client)
    agent = {k: v for k, v in AGENT.items() if k != "key"}
    first = await client.post("/agents", json=agent, headers=headers)
    second = await client.post("/agents", json=agent, headers=headers)
    for _ in range(2):
        await client.post(
            "/channels",
            json={"type": "webchat", "name": "Site", "agent_id": second.json()["id"]},
            headers=headers,
        )
    data = _manifest()
    data["channels"] = data["channels"][:1]
    assert _details(await _apply(client, headers, data)) == [
        {
            "field": "agents[0].name",
            "message": "2 agents are named 'Loja'; rename one or delete it",
        },
        {
            "field": "channels[0].name",
            "message": "2 channels are named 'Site'; rename one or delete it",
        },
    ]
    assert await _count(db_session, ManifestItem, tenant_id) == 0

    # A channel of another type with the same name is not a match: create.
    await client.delete(f"/agents/{first.json()['id']}", headers=headers)
    data["channels"] = [{**MANIFEST["channels"][1], "name": "Site"}]
    response = await _apply(client, headers, data)
    assert response.status_code == 200, response.text
    assert ("agent", "main", "adopt", []) in _actions(response.json())
    assert ("channel", "inbox", "create", []) in _actions(response.json())


async def test_an_item_deleted_by_hand_does_not_break_an_apply(client, db_session):
    headers, tenant_id = await _tenant(client)
    await _apply(client, headers, _manifest())
    tool_id = await _item_id(db_session, tenant_id, "tool", "get_order")
    billing = await _item_id(db_session, tenant_id, "department", "billing")
    assert (await client.delete(f"/tools/{tool_id}", headers=headers)).status_code == 204
    assert (await client.delete(f"/categories/{billing}", headers=headers)).status_code == 204

    data = _manifest()
    data["departments"] = data["departments"][1:]  # billing gone from the file too
    response = await _apply(client, headers, data)
    assert response.status_code == 200, response.text
    changed = [c for c in _actions(response.json()) if c[2] != "unchanged"]
    assert changed == [("tool", "get_order", "create", [])]
    assert await _item_id(db_session, tenant_id, "department", "billing") is None
    new_tool = await _item_id(db_session, tenant_id, "tool", "get_order")
    assert new_tool is not None and new_tool != tool_id
    assert await _count(db_session, ManifestItem, tenant_id) == 5
    again = await _apply(client, headers, data)
    assert {c["action"] for c in again.json()["changes"]} == {"unchanged"}


async def test_the_check_locks_the_rows_against_a_concurrent_conversation(client, test_engine):
    """Between the check and the delete no conversation can reference the
    channel: the check's `FOR UPDATE` makes the insert wait (here: time out)."""
    headers, tenant_id = await _tenant(client)
    await _apply(client, headers, _manifest())
    factory = async_sessionmaker(bind=test_engine, expire_on_commit=False)
    async with factory() as other:
        site = await _item_id(other, tenant_id, "channel", "site")
        agent_id = await _item_id(other, tenant_id, "agent", "main")
        # The contact belongs to the other channel: a contact of `site` would
        # make the check refuse the delete before the lock matters.
        inbox = await _item_id(other, tenant_id, "channel", "inbox")
        contact = Contact(tenant_id=tenant_id, channel_id=inbox, external_user_id="late")
        other.add(contact)
        await other.commit()

        async with factory() as applying:
            tenant = await applying.get(Tenant, tenant_id)
            data = _manifest()
            data["channels"] = data["channels"][1:]
            result = await apply_manifest(applying, tenant, parse_manifest(data), dry_run=True)
            assert ("channel", "site", "delete") in [
                (c.kind, c.key, c.action) for c in result.changes
            ]
            await other.execute(text("SET lock_timeout = '300ms'"))
            other.add(
                Conversation(
                    tenant_id=tenant_id, contact_id=contact.id, channel_id=site, agent_id=agent_id
                )
            )
            with pytest.raises(DBAPIError, match="lock"):
                await other.flush()
            await other.rollback()
            await applying.rollback()


async def test_a_channel_with_a_contact_but_no_conversation_is_refused(client, db_session):
    """Deleting a channel erases its contacts and their memories by cascade."""
    headers, tenant_id = await _tenant(client)
    await _apply(client, headers, _manifest())
    site = await _item_id(db_session, tenant_id, "channel", "site")
    inbox = await _item_id(db_session, tenant_id, "channel", "inbox")
    for user in ("a", "b", "c"):
        db_session.add(Contact(tenant_id=tenant_id, channel_id=site, external_user_id=user))
    db_session.add(Contact(tenant_id=tenant_id, channel_id=inbox, external_user_id="solo"))
    await db_session.commit()

    for dry_run in (True, False):
        response = await _apply(client, headers, {"version": 1}, dry_run=dry_run)
        details = _details(response)
        assert {
            "field": "channels",
            "message": "channel 'site' has 3 contacts; deleting it would erase them "
            "and their memories",
        } in details
        assert {
            "field": "channels",
            "message": "channel 'inbox' has 1 contact; deleting it would erase it "
            "and its memories",
        } in details
    assert await db_session.get(Channel, site) is not None
    assert await _count(db_session, Contact, tenant_id) == 4
    assert await _count(db_session, ManifestItem, tenant_id) == 6
