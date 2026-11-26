"""The example manifests in `examples/manifests/` are valid and apply to an
empty tenant once the secrets they name are set."""

import os
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.manifest import yaml_io
from app.manifest.apply import parse_manifest, secret_references
from app.models.agent import Agent
from app.models.category import Category
from app.models.channel import Channel
from app.models.tool import Tool

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "manifests"

# file name -> (secrets it names, items it creates: agents, departments, tools, channels)
EXPECTED = {
    "store": ({"store_orders_token"}, (1, 3, 1, 1)),
    "clinic": ({"clinic_agenda_key"}, (1, 4, 1, 1)),
    "law-firm": (set(), (1, 4, 0, 1)),
}


def _text(name: str) -> str:
    return (EXAMPLES / f"{name}.yaml").read_text()


def test_the_examples_on_disk_are_the_ones_tested():
    assert sorted(p.stem for p in EXAMPLES.glob("*.yaml")) == sorted(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_example_validates_against_the_schema(name):
    manifest = parse_manifest(yaml_io.load(_text(name)))
    assert {secret for _, secret in secret_references(manifest)} == EXPECTED[name][0]
    assert len(manifest.agents) == 1 and len(manifest.channels) == 1
    assert len(manifest.departments) >= 2
    assert all(d.description for d in manifest.departments)
    assert {c.agent for c in manifest.channels} == {a.key for a in manifest.agents}


@pytest.mark.parametrize("name", sorted(EXPECTED))
async def test_example_applies_to_an_empty_tenant_and_twice_changes_nothing(
    client, db_session, name
):
    created = await client.post(
        "/tenants", json={"name": name}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": created.json()["api_key"]}
    tenant_id = created.json()["id"]
    yaml_headers = {**headers, "Content-Type": "application/yaml"}
    text = _text(name).encode()

    secrets, counts = EXPECTED[name]
    # Without its secrets the file is refused and names each one.
    if secrets:
        refused = await client.post(
            "/manifest/apply", content=text, headers=yaml_headers
        )
        assert refused.status_code == 422
        messages = {d["message"] for d in refused.json()["error"]["details"]}
        assert messages == {f"secret '{s}' is not set" for s in secrets}
    for secret in secrets:
        put = await client.put(f"/secrets/{secret}", json={"value": "tok-123"}, headers=headers)
        assert put.status_code == 204

    dry = await client.post("/manifest/apply?dry_run=true", content=text, headers=yaml_headers)
    assert dry.status_code == 200, dry.text
    assert {c["action"] for c in dry.json()["changes"]} == {"create"}

    applied = await client.post("/manifest/apply", content=text, headers=yaml_headers)
    assert applied.status_code == 200, applied.text
    assert {c["action"] for c in applied.json()["changes"]} == {"create"}
    external = {
        c.key for c in parse_manifest(yaml_io.load(_text(name))).channels if c.type == "external"
    }
    generated = applied.json()["generated"].get("channels", {})
    assert set(generated) == external

    for model, expected in zip((Agent, Category, Tool, Channel), counts, strict=True):
        found = await db_session.scalar(
            select(func.count()).select_from(model).where(model.tenant_id == tenant_id)
        )
        assert found == expected, model.__name__

    again = await client.post("/manifest/apply", content=text, headers=yaml_headers)
    assert again.status_code == 200
    assert {c["action"] for c in again.json()["changes"]} == {"unchanged"}
    assert again.json()["generated"] == {}
