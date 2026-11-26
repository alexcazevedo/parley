"""The manifest's `knowledge` section: entries and question-and-answer pairs
are applied, adopted, updated, removed and exported; documents never."""

import os
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.knowledge.ingest import process_due
from app.models.knowledge_item import KnowledgeItem, KnowledgeItemKind, KnowledgeItemStatus
from app.models.knowledge_passage import KnowledgePassage

FILE = {
    "version": 1,
    "knowledge": {
        "entries": [
            {"title": "Horário", "text": "Abrimos de segunda a sexta, das nove às dezoito."},
            {"title": "Garantia", "text": "Todo produto tem garantia de um ano."},
        ],
        "qa": [{"question": "Qual o prazo de entrega?", "answer": "Entregamos em cinco dias."}],
    },
}


@pytest.fixture
def factory(test_engine):
    return async_sessionmaker(bind=test_engine, expire_on_commit=False)


async def _tenant(client) -> tuple[dict, uuid.UUID]:
    response = await client.post(
        "/tenants", json={"name": "Loja"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    body = response.json()
    return {"X-API-Key": body["api_key"]}, uuid.UUID(body["id"])


async def _apply(client, headers, data, *, dry_run=False):
    return await client.post(
        f"/manifest/apply?dry_run={'true' if dry_run else 'false'}", json=data, headers=headers
    )


def _actions(response) -> list[tuple[str, str, str, list[str]]]:
    assert response.status_code == 200, response.text
    return [(c["kind"], c["key"], c["action"], c["fields"]) for c in response.json()["changes"]]


async def _items(factory, tenant_id) -> list[KnowledgeItem]:
    async with factory() as session:
        rows = await session.scalars(
            select(KnowledgeItem)
            .where(KnowledgeItem.tenant_id == tenant_id)
            .order_by(KnowledgeItem.title)
        )
        return list(rows.all())


async def _passage_count(factory, tenant_id) -> int:
    async with factory() as session:
        return int(
            await session.scalar(
                select(func.count())
                .select_from(KnowledgePassage)
                .where(KnowledgePassage.tenant_id == tenant_id)
            )
            or 0
        )


async def _search(client, headers, query: str) -> list[dict]:
    response = await client.post("/knowledge/search", json={"query": query}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["results"]


def _file(entries=None, qa=None) -> dict:
    return {
        "version": 1,
        "knowledge": {
            "entries": FILE["knowledge"]["entries"] if entries is None else entries,
            "qa": FILE["knowledge"]["qa"] if qa is None else qa,
        },
    }


async def test_create_search_unchanged_update_and_remove(client, factory):
    headers, tenant_id = await _tenant(client)
    dry = await _apply(client, headers, FILE, dry_run=True)
    assert _actions(dry) == [
        ("knowledge_entry", "Horário", "create", []),
        ("knowledge_entry", "Garantia", "create", []),
        ("knowledge_qa", "Qual o prazo de entrega?", "create", []),
    ]
    assert await _items(factory, tenant_id) == []

    first = await _apply(client, headers, FILE)
    assert [a[2] for a in _actions(first)] == ["create"] * 3
    assert all(c["id"] for c in first.json()["changes"])
    items = await _items(factory, tenant_id)
    assert {i.status for i in items} == {KnowledgeItemStatus.PROCESSING}
    assert {i.kind for i in items} == {KnowledgeItemKind.ENTRY, KnowledgeItemKind.QA}

    assert await process_due(factory) == 3
    assert [r["title"] for r in await _search(client, headers, "garantia")] == ["Garantia"]
    assert [r["title"] for r in await _search(client, headers, "entrega")] == [
        "Qual o prazo de entrega?"
    ]

    again = await _apply(client, headers, FILE)
    assert [a[2] for a in _actions(again)] == ["unchanged"] * 3

    changed = _file(
        entries=[
            FILE["knowledge"]["entries"][0],
            {"title": "Garantia", "text": "Todo produto tem garantia de dois anos."},
        ]
    )
    update = await _apply(client, headers, changed)
    assert ("knowledge_entry", "Garantia", "update", ["text"]) in _actions(update)
    row = next(i for i in await _items(factory, tenant_id) if i.title == "Garantia")
    assert row.status == KnowledgeItemStatus.PROCESSING
    await process_due(factory)
    hits = await _search(client, headers, "dois anos")
    assert hits and "dois anos" in hits[0]["text"]
    assert await _search(client, headers, "um ano") == [] or all(
        "um ano" not in h["text"] for h in await _search(client, headers, "um ano")
    )

    removed = _file(entries=[FILE["knowledge"]["entries"][0]], qa=[])
    gone = await _apply(client, headers, removed)
    assert sorted((a[1], a[2]) for a in _actions(gone) if a[2] == "delete") == [
        ("Garantia", "delete"),
        ("Qual o prazo de entrega?", "delete"),
    ]
    assert [i.title for i in await _items(factory, tenant_id)] == ["Horário"]
    assert await _search(client, headers, "garantia") == []
    assert await _passage_count(factory, tenant_id) == 1


async def test_a_hand_made_item_of_the_same_title_is_adopted(client, factory):
    headers, tenant_id = await _tenant(client)
    made = await client.post(
        "/knowledge/entries", json={"title": "Horário", "text": "Texto antigo."}, headers=headers
    )
    assert made.status_code == 201
    other = await client.post(
        "/knowledge/entries", json={"title": "Feito à mão", "text": "Fica."}, headers=headers
    )
    assert other.status_code == 201
    await process_due(factory)

    response = await _apply(client, headers, FILE)
    assert ("knowledge_entry", "Horário", "adopt", ["text"]) in _actions(response)
    adopted = next(c for c in response.json()["changes"] if c["key"] == "Horário")
    assert adopted["id"] == made.json()["id"]
    row = next(i for i in await _items(factory, tenant_id) if i.title == "Horário")
    assert row.status == KnowledgeItemStatus.PROCESSING

    # The hand-made item the file does not name survives every apply.
    await _apply(client, headers, {"version": 1, "knowledge": {}})
    assert [i.title for i in await _items(factory, tenant_id)] == ["Feito à mão"]


async def test_a_file_without_the_section_leaves_knowledge_alone(client, factory):
    headers, tenant_id = await _tenant(client)
    await _apply(client, headers, FILE)
    old = await _apply(client, headers, {"version": 1})
    assert _actions(old) == []
    assert len(await _items(factory, tenant_id)) == 3


async def test_a_document_survives_and_is_only_an_export_warning(
    client, factory, tmp_path, monkeypatch
):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    try:
        headers, tenant_id = await _tenant(client)
        upload = await client.post(
            "/knowledge/documents",
            files={"file": ("manual.txt", b"Manual de uso do produto.", "text/plain")},
            headers=headers,
        )
        assert upload.status_code == 201, upload.text
        await _apply(client, headers, FILE)
        await _apply(client, headers, {"version": 1, "knowledge": {}})
        await _apply(client, headers, {"version": 1})
        kinds = [i.kind for i in await _items(factory, tenant_id)]
        assert kinds == [KnowledgeItemKind.DOCUMENT]

        await _apply(client, headers, FILE)
        exported = (await client.get("/manifest", headers=headers)).json()
        assert exported["warnings"] == [
            "document 'manual.txt' is not in the manifest: "
            "documents are uploaded through the API"
        ]
        assert "document" not in str(exported["manifest"]["knowledge"])
    finally:
        get_settings.cache_clear()


async def test_export_then_apply_on_an_empty_tenant_reproduces_the_knowledge(client, factory):
    headers, _ = await _tenant(client)
    await _apply(client, headers, FILE)
    exported = (await client.get("/manifest", headers=headers)).json()["manifest"]
    assert [e["title"] for e in exported["knowledge"]["entries"]] == ["Garantia", "Horário"]
    assert exported["knowledge"]["qa"] == FILE["knowledge"]["qa"]

    other, other_id = await _tenant(client)
    response = await _apply(client, other, exported)
    assert [a[2] for a in _actions(response)] == ["create"] * 3
    assert {i.title: i.body for i in await _items(factory, other_id)} == {
        **{e["title"]: e["text"] for e in FILE["knowledge"]["entries"]},
        FILE["knowledge"]["qa"][0]["question"]: FILE["knowledge"]["qa"][0]["answer"],
    }
    again = await _apply(client, other, exported)
    assert [a[2] for a in _actions(again)] == ["unchanged"] * 3


async def test_duplicates_unknown_keys_and_caps_are_refused(client):
    headers, _ = await _tenant(client)
    entry = {"title": "A", "text": "x"}
    dup = await _apply(client, headers, _file(entries=[entry, entry], qa=[]))
    assert dup.status_code == 422
    assert {"field": "knowledge.entries[1].title", "message": "duplicate name in the file"} in (
        dup.json()["error"]["details"]
    )
    dup_qa = await _apply(
        client, headers, _file(entries=[], qa=[{"question": "Q", "answer": "a"}] * 2)
    )
    assert dup_qa.json()["error"]["details"][0]["field"] == "knowledge.qa[1].question"

    extra = await _apply(
        client, headers, {"version": 1, "knowledge": {"entries": [], "documents": []}}
    )
    assert extra.status_code == 422
    assert extra.json()["error"]["details"][0]["field"] == "knowledge.documents"
    extra_key = await _apply(
        client,
        headers,
        {"version": 1, "knowledge": {"entries": [{"title": "a", "text": "b", "x": 1}]}},
    )
    assert extra_key.status_code == 422

    many = await _apply(
        client, headers, _file(entries=[{"title": f"t{i}", "text": "x"} for i in range(501)])
    )
    assert many.status_code == 422


@pytest.mark.parametrize(
    ("setting", "value"),
    [
        ("KNOWLEDGE_MAX_ITEMS", "2"),
        ("KNOWLEDGE_MAX_ITEM_CHARS", "30"),
        ("KNOWLEDGE_MAX_TOTAL_CHARS", "100"),
    ],
)
async def test_a_limit_is_a_problem_and_nothing_is_written(
    client, factory, monkeypatch, setting, value
):
    headers, tenant_id = await _tenant(client)
    monkeypatch.setenv(setting, value)
    get_settings.cache_clear()
    try:
        for dry_run in (True, False):
            response = await _apply(client, headers, FILE, dry_run=dry_run)
            assert response.status_code == 422, response.text
            assert response.json()["error"]["details"] == [
                {
                    "field": "knowledge",
                    "message": f"knowledge limit exceeded: {setting} is {value}",
                }
            ]
        assert await _items(factory, tenant_id) == []
    finally:
        get_settings.cache_clear()


async def test_knowledge_enabled_travels_in_the_agents(client):
    headers, _ = await _tenant(client)
    agent = {
        "key": "main",
        "name": "Loja",
        "instructions": "Atenda.",
        "model": "gpt-4o-mini",
        "knowledge_enabled": True,
    }
    response = await _apply(client, headers, {"version": 1, "agents": [agent], **_file()})
    assert response.status_code == 200, response.text
    listed = (await client.get("/agents", headers=headers)).json()
    assert [a["knowledge_enabled"] for a in listed] == [True]
    exported = (await client.get("/manifest", headers=headers)).json()["manifest"]
    assert exported["agents"][0]["knowledge_enabled"] is True
