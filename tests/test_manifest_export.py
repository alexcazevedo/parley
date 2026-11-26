"""Manifest export (`GET /manifest`) and YAML in and out: a hand-made tenant is
exported without a credential, and the export applies back to the same tenant."""

import os
import uuid

import yaml
from sqlalchemy import select

from app.manifest import yaml_io
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.manifest_item import ManifestItem

TOKEN = "tok-EXPORT-9f3a1c"
EVO = "evo-EXPORT-77c1d2"
ORDERS_URL = "https://orders.example.com/orders"
EVENTS_URL = "https://inbox-export.example.com/events"
YAML_HEADERS = {"Content-Type": "application/yaml"}

TOOL = {
    "name": "get_order",
    "description": "Look up an order",
    "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}},
    "endpoint": ORDERS_URL,
    "method": "GET",
    "auth_config": {"type": "bearer", "token": TOKEN},
}
AGENT = {
    "name": "Loja",
    "instructions": "Você é o atendente da Loja.\nSeja breve.\n",
    "model": "gpt-4o-mini",
    "tool_names": ["get_order"],
    "temperature": 0.2,
    "boundary_never": ["política"],
}


async def _tenant(client) -> tuple[dict, uuid.UUID]:
    response = await client.post(
        "/tenants", json={"name": "Loja"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    body = response.json()
    return {"X-API-Key": body["api_key"]}, uuid.UUID(body["id"])


async def _hand_made(client, headers) -> dict:
    """Agent with a bearer-token tool, two departments, a web chat, an Evolution
    and an external channel, all through the API. Returns the events secret."""
    assert (await client.post("/tools", json=TOOL, headers=headers)).status_code == 201
    for name in ("billing", "shipping"):
        body = {"name": name, **({"description": "Pagamentos"} if name == "billing" else {})}
        assert (await client.post("/categories", json=body, headers=headers)).status_code == 201
    created = await client.post("/agents", json=AGENT, headers=headers)
    assert created.status_code == 201, created.text
    agent_id = created.json()["id"]
    channels = [
        {"type": "webchat", "name": "Site", "agent_id": agent_id},
        {
            "type": "whatsapp_evolution",
            "name": "WhatsApp Loja",
            "agent_id": agent_id,
            "config": {"base_url": "https://evo.example.com", "instance": "loja"},
            "secret": EVO,
        },
        {
            "type": "external",
            "name": "Inbox",
            "agent_id": agent_id,
            "config": {"events_url": EVENTS_URL},
        },
    ]
    events_secret = ""
    for channel in channels:
        response = await client.post("/channels", json=channel, headers=headers)
        assert response.status_code == 201, response.text
        events_secret = response.json().get("events_secret") or events_secret
    assert events_secret
    return {"events_secret": events_secret}


async def _put_secrets(client, headers, names) -> None:
    values = {"tool_get_order_token": TOKEN, "channel_whatsapp_loja_secret": EVO}
    for name in names:
        response = await client.put(
            f"/secrets/{name}", json={"value": values.get(name, EVO)}, headers=headers
        )
        assert response.status_code == 204, response.text


def _no_credentials(text: str, hand: dict) -> None:
    for value in (TOKEN, EVO, hand["events_secret"]):
        assert value not in text


def _apply_json(client, headers, manifest, dry_run):
    return client.post(
        f"/manifest/apply?dry_run={'true' if dry_run else 'false'}", json=manifest, headers=headers
    )


def _apply_yaml(client, headers, text, dry_run):
    return client.post(
        f"/manifest/apply?dry_run={'true' if dry_run else 'false'}",
        content=text,
        headers={**headers, **YAML_HEADERS},
    )


def _changes(response) -> list[tuple[str, str, str, list[str]]]:
    assert response.status_code == 200, response.text
    return [(c["kind"], c["key"], c["action"], c["fields"]) for c in response.json()["changes"]]


async def test_a_hand_made_tenant_exports_without_a_credential_and_applies_back(client, db_session):
    headers, tenant_id = await _tenant(client)
    hand = await _hand_made(client, headers)

    response = await client.get("/manifest", headers=headers)
    assert response.status_code == 200, response.text
    _no_credentials(response.text, hand)
    body = response.json()
    assert set(body) == {"manifest", "secrets_needed", "warnings"}
    assert body["warnings"] == []
    manifest = body["manifest"]
    assert list(manifest) == ["version", "agents", "departments", "tools", "channels"]
    # Defaults are left out; the order is the schema's.
    assert manifest["agents"] == [
        {
            "key": "loja",
            "name": "Loja",
            "instructions": AGENT["instructions"],
            "model": "gpt-4o-mini",
            "temperature": 0.2,
            "tool_names": ["get_order"],
            "boundary_never": ["política"],
        }
    ]
    assert manifest["departments"] == [
        {"name": "billing", "description": "Pagamentos"},
        {"name": "shipping"},
    ]
    assert manifest["tools"] == [
        {
            "name": "get_order",
            "description": "Look up an order",
            "input_schema": TOOL["input_schema"],
            "endpoint": ORDERS_URL,
            "method": "GET",
            "auth_config": {"type": "bearer", "token": {"secret": "tool_get_order_token"}},
        }
    ]
    assert manifest["channels"] == [
        {
            "key": "inbox",
            "type": "external",
            "name": "Inbox",
            "agent": "loja",
            "config": {"events_url": EVENTS_URL},
        },
        {"key": "site", "type": "webchat", "name": "Site", "agent": "loja"},
        {
            "key": "whatsapp-loja",
            "type": "whatsapp_evolution",
            "name": "WhatsApp Loja",
            "agent": "loja",
            "config": {"base_url": "https://evo.example.com", "instance": "loja"},
            "secret": {"secret": "channel_whatsapp_loja_secret"},
        },
    ]
    assert body["secrets_needed"] == ["channel_whatsapp_loja_secret", "tool_get_order_token"]

    yaml_response = await client.get("/manifest?format=yaml", headers=headers)
    assert yaml_response.status_code == 200
    assert yaml_response.headers["content-type"].startswith("application/yaml")
    _no_credentials(yaml_response.text, hand)
    assert yaml_response.text.startswith(
        "# secrets_needed: channel_whatsapp_loja_secret, tool_get_order_token\n"
    )
    assert yaml.safe_load(yaml_response.text) == manifest
    assert "instructions: |\n" in yaml_response.text

    # Unset secrets are named by a dry run; set, the dry run only adopts.
    refused = await _apply_json(client, headers, manifest, True)
    assert refused.status_code == 422
    await _put_secrets(client, headers, ["channel_whatsapp_loja_secret", "tool_get_order_token"])
    again = (await client.get("/manifest", headers=headers)).json()
    assert again["secrets_needed"] == []
    assert again["manifest"] == manifest

    dry = await _apply_json(client, headers, manifest, True)
    adopted = _changes(dry)
    assert {a for _, _, a, _ in adopted} == {"adopt"}
    assert all(fields == [] for _, _, _, fields in adopted)
    assert len(adopted) == 7
    assert (await _apply_json(client, headers, manifest, False)).status_code == 200
    assert {a for _, _, a, _ in _changes(await _apply_json(client, headers, manifest, True))} == {
        "unchanged"
    }
    keys = set(
        await db_session.scalars(
            select(ManifestItem.key).where(ManifestItem.tenant_id == tenant_id)
        )
    )
    assert keys == {"loja", "billing", "shipping", "get_order", "inbox", "site", "whatsapp-loja"}
    assert (await client.get("/manifest", headers=headers)).json()["manifest"] == manifest


async def test_the_yaml_export_applies_as_yaml_to_the_same_result(client, db_session):
    headers, tenant_id = await _tenant(client)
    hand = await _hand_made(client, headers)
    await _put_secrets(client, headers, ["channel_whatsapp_loja_secret", "tool_get_order_token"])
    exported = await client.get("/manifest?format=yaml", headers=headers)
    _no_credentials(exported.text, hand)

    dry = _changes(await _apply_yaml(client, headers, exported.text, True))
    assert {a for _, _, a, _ in dry} == {"adopt"}
    assert all(fields == [] for _, _, _, fields in dry)
    response = await _apply_yaml(client, headers, exported.text, False)
    assert _changes(response) == dry
    assert {
        a for _, _, a, _ in _changes(await _apply_yaml(client, headers, exported.text, True))
    } == {"unchanged"}
    agent = await db_session.scalar(select(Agent).where(Agent.tenant_id == tenant_id))
    assert agent.instructions == AGENT["instructions"]
    assert await db_session.scalar(
        select(Channel.id).where(Channel.tenant_id == tenant_id, Channel.name == "Inbox")
    )


async def test_unmanaged_keys_are_slugged_and_never_clash_with_managed_ones(client, db_session):
    headers, _ = await _tenant(client)
    managed = {
        "version": 1,
        "agents": [{"key": "sales-2", "name": "Managed", "instructions": "x", "model": "m"}],
    }
    assert (await _apply_json(client, headers, managed, False)).status_code == 200
    agent = {"instructions": "x", "model": "m"}
    for name in ("Sales", "Sales!", "sales", "Ação / Ñ", "!!!"):
        assert (
            await client.post("/agents", json={**agent, "name": name}, headers=headers)
        ).status_code == 201
    body = (await client.get("/manifest", headers=headers)).json()
    by_key = {a["key"]: a["name"] for a in body["manifest"]["agents"]}
    assert by_key == {
        "sales-2": "Managed",
        "sales": "Sales",
        "sales-3": "Sales!",
        "sales-4": "sales",
        "a-o": "Ação / Ñ",
        "item": "!!!",
    }
    assert [a["key"] for a in body["manifest"]["agents"]] == sorted(by_key)
    assert body["warnings"] == []


async def test_same_named_channels_and_agents_warn_and_still_export(client):
    headers, _ = await _tenant(client)
    agent = {"instructions": "x", "model": "m", "name": "Sales"}
    ids = [
        (await client.post("/agents", json=agent, headers=headers)).json()["id"] for _ in range(2)
    ]
    for _ in range(2):
        created = await client.post(
            "/channels",
            json={"type": "webchat", "name": "Site", "agent_id": ids[0]},
            headers=headers,
        )
        assert created.status_code == 201
    response = await client.get("/manifest", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["warnings"] == [
        "2 agents are named 'Sales'; rename one or delete it before applying the manifest",
        "2 channels of type webchat are named 'Site'; rename one or delete it "
        "before applying the manifest",
    ]
    assert [a["key"] for a in body["manifest"]["agents"]] == ["sales", "sales-2"]
    assert [c["key"] for c in body["manifest"]["channels"]] == ["site", "site-2"]
    text = (await client.get("/manifest?format=yaml", headers=headers)).text
    assert "# warning: 2 agents are named 'Sales'" in text
    assert (await client.get("/manifest?format=xml", headers=headers)).status_code == 422


async def test_an_empty_tenant_exports_an_empty_manifest(client):
    headers, _ = await _tenant(client)
    body = (await client.get("/manifest", headers=headers)).json()
    assert body == {"manifest": {"version": 1}, "secrets_needed": [], "warnings": []}
    assert (await client.get("/manifest")).status_code == 401


async def test_the_export_is_the_callers_own(client):
    headers, _ = await _tenant(client)
    other, _ = await _tenant(client)
    await _hand_made(client, other)
    assert (await client.get("/manifest", headers=headers)).json()["manifest"] == {"version": 1}


# YAML input


async def test_a_python_object_tag_is_refused(client):
    headers, _ = await _tenant(client)
    text = "version: 1\ndepartments:\n  - name: !!python/object/apply:os.system ['echo hi']\n"
    response = await _apply_yaml(client, headers, text, True)
    assert response.status_code == 422
    detail = response.json()["error"]["details"][0]
    assert detail["message"].startswith("invalid YAML, line 3: ")
    assert "python/object" in detail["message"]


async def test_aliases_and_anchors_are_refused(client):
    headers, _ = await _tenant(client)
    bomb = "version: 1\nx: &a [1, 2]\ny: &b [*a, *a]\nz: &c [*b, *b, *b, *b]\n"
    for text in (bomb, "version: 1\ndepartments:\n  - &d {name: a}\n  - *d\n", "a: &x 1\n"):
        response = await _apply_yaml(client, headers, text, True)
        assert response.status_code == 422, text
        assert "anchors and aliases are not allowed" in response.text
    assert yaml_io.dump({"a": {"k": 1}, "b": {"k": 1}}).count("&") == 0


async def test_implicit_yaml_types_fail_validation_at_their_path(client):
    headers, _ = await _tenant(client)
    text = (
        "version: 1\n"
        "agents:\n"
        "  - key: a\n"
        "    name: no\n"
        "    instructions: 1.0\n"
        "    model: 7\n"
        "departments:\n"
        "  - name: on\n"
    )
    response = await _apply_yaml(client, headers, text, True)
    assert response.status_code == 422
    details = response.json()["error"]["details"]
    assert {d["field"] for d in details} == {
        "agents[0].name",
        "agents[0].instructions",
        "agents[0].model",
        "departments[0].name",
    }
    assert {d["message"] for d in details} == {
        "Input should be a valid string (in YAML, quote the value)"
    }
    # A date is refused by the loader, at its path.
    dated = "version: 1\ndepartments:\n  - name: 2024-01-31\n"
    response = await _apply_yaml(client, headers, dated, True)
    assert response.status_code == 422
    assert response.json()["error"]["details"] == [
        {
            "field": "departments[0].name",
            "message": "invalid YAML, a date value is not allowed; use a quoted string",
        }
    ]
    # Quoted, they are strings.
    ok = 'version: 1\ndepartments:\n  - name: "no"\n  - name: "1.0"\n'
    assert _changes(await _apply_yaml(client, headers, ok, True)) == [
        ("department", "no", "create", []),
        ("department", "1.0", "create", []),
    ]


async def test_yaml_values_json_cannot_hold_are_refused(client):
    headers, _ = await _tenant(client)
    head = "version: 1\ntools:\n  - name: t\n    endpoint: https://a.example.com\n"
    for schema, path, fragment in (
        ("{since: 2024-01-31}", "tools[0].input_schema.since", "a date value"),
        ("{x: .inf}", "tools[0].input_schema.x", "not a finite number"),
        ("{1: a}", "tools[0].input_schema", "a key is not a string"),
        ("{b: !!binary aGk=}", "tools[0].input_schema.b", "a bytes value"),
    ):
        response = await _apply_yaml(client, headers, f"{head}    input_schema: {schema}\n", True)
        assert response.status_code == 422, schema
        detail = response.json()["error"]["details"][0]
        assert detail["field"] == path
        assert fragment in detail["message"]


async def test_malformed_yaml_and_non_mappings_are_a_422_with_the_line(client):
    headers, _ = await _tenant(client)
    response = await _apply_yaml(client, headers, "version: 1\nagents: [\n  a: b\n", True)
    assert response.status_code == 422
    message = response.json()["error"]["details"][0]["message"]
    assert message.startswith("invalid YAML, line ")
    for text in ("- a\n- b\n", "just text\n", "", "---\na: 1\n---\nb: 2\n"):
        response = await _apply_yaml(client, headers, text, True)
        assert response.status_code == 422, text
    assert (
        "line 1: the document must be a mapping"
        in (await _apply_yaml(client, headers, "- a\n", True)).json()["error"]["details"][0][
            "message"
        ]
    )
    deep = "[" * 200_000
    response = await _apply_yaml(client, headers, deep, True)
    assert response.status_code == 422
    garbled = await client.post(
        "/manifest/apply",
        content=b"\xff\xfe",
        headers={**headers, **YAML_HEADERS},
    )
    assert garbled.status_code == 422


async def test_content_types_and_json_errors(client):
    headers, _ = await _tenant(client)
    ok = "version: 1\n"
    for content_type in ("text/yaml", "application/yaml; charset=utf-8", "Application/YAML"):
        response = await client.post(
            "/manifest/apply?dry_run=true",
            content=ok,
            headers={**headers, "Content-Type": content_type},
        )
        assert response.status_code == 200, content_type
    response = await client.post(
        "/manifest/apply", content=ok, headers={**headers, "Content-Type": "text/plain"}
    )
    assert response.status_code == 415
    broken = await client.post(
        "/manifest/apply",
        content='{"version": 1,',
        headers={**headers, "Content-Type": "application/json"},
    )
    assert broken.status_code == 422
    assert broken.json()["error"]["details"][0]["message"].startswith("invalid JSON, line 1")
    assert (await _apply_json(client, headers, [1], True)).status_code == 422


async def test_an_oversized_body_is_a_413_in_both_formats(client):
    headers, _ = await _tenant(client)
    pad = "x" * 1_100_000
    big_yaml = f"version: 1\nnote: {pad}\n"
    response = await _apply_yaml(client, headers, big_yaml, True)
    assert response.status_code == 413
    big_json = await client.post(
        "/manifest/apply", json={"version": 1, "note": pad}, headers=headers
    )
    assert big_json.status_code == 413

    async def chunks():  # no Content-Length: counted as received
        for _ in range(12):
            yield b"#" * 100_000

    streamed = await client.post(
        "/manifest/apply", content=chunks(), headers={**headers, **YAML_HEADERS}
    )
    assert streamed.status_code == 413
    # Just under the cap is read (and fails validation, not size).
    under = f"version: 1\n# {'x' * 1_000_000}\n"
    assert (await _apply_yaml(client, headers, under, True)).status_code == 200


async def test_the_dump_round_trips_awkward_strings():
    manifest = {
        "version": 1,
        "departments": [
            {"name": "no", "description": "line one\nline two  \n\ttabbed"},
            {"name": "1.0"},
            {"name": "2024-01-31"},
            {"name": "ação: ñ # not a comment"},
        ],
    }
    text = yaml_io.dump(manifest)
    assert yaml_io.load(text) == manifest
    assert "&" not in text
