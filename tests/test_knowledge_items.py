"""Knowledge items: create, split in the worker (`process_due`), keyword search,
edit, delete, limits, duplicate titles, paging and tenant isolation."""

import os
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.knowledge.ingest import process_due
from app.knowledge.search import RESULT_BUDGET_CHARS
from app.knowledge.text import PASSAGE_CHARS, normalize, split_passages
from app.models.knowledge_passage import KnowledgePassage


@pytest.fixture
def factory(test_engine):
    return async_sessionmaker(bind=test_engine, expire_on_commit=False)


async def _tenant(client, name: str) -> dict:
    response = await client.post(
        "/tenants", json={"name": name}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    return {"X-API-Key": response.json()["api_key"]}


async def _entry(client, headers, title: str, text: str) -> dict:
    response = await client.post(
        "/knowledge/entries", json={"title": title, "text": text}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _search(client, headers, query: str) -> list[dict]:
    response = await client.post("/knowledge/search", json={"query": query}, headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "keyword"
    return response.json()["results"]


async def _passages(factory, item_id: str) -> list[KnowledgePassage]:
    async with factory() as session:
        rows = await session.scalars(
            select(KnowledgePassage)
            .where(KnowledgePassage.item_id == uuid.UUID(item_id))
            .order_by(KnowledgePassage.position)
        )
        return list(rows.all())


def test_normalize_and_split():
    assert normalize("  Horário  de Atenção ") == "horario de atencao"
    assert split_passages("Q?", "short answer") == ["short answer"]
    para = "Frase número um. " * 100
    parts = split_passages("Title", f"Intro.\n\n{para}\n\nEnd.")
    assert len(parts) > 1
    assert all(len(p) + len("Title") + 1 <= PASSAGE_CHARS for p in parts)
    assert all(p.endswith(".") for p in parts)


async def test_entry_and_pair_become_ready_and_searchable(client, factory):
    headers = await _tenant(client, "Acme")
    entry = await _entry(client, headers, "Horário", "Abrimos às 9h e fechamos às 18h.")
    assert entry["status"] == "processing"
    assert entry["kind"] == "entry" and entry["text"].startswith("Abrimos")
    assert entry["passages"] == 0 and entry["chars"] == len("Abrimos às 9h e fechamos às 18h.")
    assert set(entry) == {
        "id", "kind", "title", "text", "filename", "status", "error", "chars",
        "passages", "truncated", "created_at", "updated_at",
    }
    pair = await client.post(
        "/knowledge/qa",
        json={"question": "Vocês entregam?", "answer": "Sim, entregamos em toda a cidade."},
        headers=headers,
    )
    assert pair.status_code == 201
    pair_id = pair.json()["id"]
    assert pair.json()["kind"] == "qa" and pair.json()["title"] == "Vocês entregam?"

    # Items other tests left in `processing` may be taken in the same pass.
    assert await process_due(factory) >= 2
    assert await process_due(factory) == 0

    got = (await client.get(f"/knowledge/{entry['id']}", headers=headers)).json()
    assert got["status"] == "ready" and got["passages"] == 1
    got_pair = (await client.get(f"/knowledge/{pair_id}", headers=headers)).json()
    assert got_pair["status"] == "ready" and got_pair["passages"] == 1

    for query in ("horario", "horário", "HORÁRIO de funcionamento"):
        hits = await _search(client, headers, query)
        assert [h["item_id"] for h in hits] == [entry["id"]], query
    hit = hits[0]
    assert hit["title"] == "Horário" and hit["kind"] == "entry"
    assert "Abrimos" in hit["text"] and hit["keyword_rank"] == 1
    assert hit["semantic_rank"] is None and hit["score"] > 0

    delivered = await _search(client, headers, "entregam na cidade")
    assert delivered[0]["item_id"] == pair_id
    assert await _search(client, headers, "o de a") == []


async def test_edit_replaces_passages_and_delete_removes_them(client, factory):
    headers = await _tenant(client, "Acme")
    entry = await _entry(client, headers, "Trocas", "Trocas em até sete dias.")
    await process_due(factory)
    old = await _passages(factory, entry["id"])
    assert len(old) == 1

    edited = await client.patch(
        f"/knowledge/{entry['id']}", json={"text": "Devoluções aceitas com nota fiscal."},
        headers=headers,
    )
    assert edited.status_code == 200
    assert edited.json()["status"] == "processing"
    # Old passages stay searchable until the worker replaces them.
    assert len(await _search(client, headers, "sete dias")) == 1

    await process_due(factory)
    new = await _passages(factory, entry["id"])
    assert len(new) == 1 and new[0].id != old[0].id
    assert "nota fiscal" in new[0].content
    assert await _search(client, headers, "sete") == []
    assert len(await _search(client, headers, "devolucoes")) == 1

    renamed = await client.patch(
        f"/knowledge/{entry['id']}", json={"title": "Política de devolução"}, headers=headers
    )
    assert renamed.json()["status"] == "processing"
    await process_due(factory)
    assert (await _search(client, headers, "politica"))[0]["title"] == "Política de devolução"

    assert (await client.delete(f"/knowledge/{entry['id']}", headers=headers)).status_code == 204
    assert await _passages(factory, entry["id"]) == []
    assert (await client.get(f"/knowledge/{entry['id']}", headers=headers)).status_code == 404
    assert await _search(client, headers, "devolucoes") == []


async def test_search_budget(client, factory):
    headers = await _tenant(client, "Acme")
    sentence = "Pagamento por pix aceito sempre. "
    for i in range(6):
        await _entry(client, headers, f"Pagamento {i}", sentence * 20)
    await process_due(factory)
    hits = await _search(client, headers, "pix")
    assert 1 <= len(hits) <= 4
    assert sum(len(h["text"]) for h in hits) <= RESULT_BUDGET_CHARS


async def test_limits_name_their_setting(client, monkeypatch):
    headers = await _tenant(client, "Acme")
    settings = get_settings()
    monkeypatch.setattr(settings, "knowledge_max_item_chars", 10)
    too_long = await client.post(
        "/knowledge/entries", json={"title": "a", "text": "x" * 11}, headers=headers
    )
    assert too_long.status_code == 422
    assert "KNOWLEDGE_MAX_ITEM_CHARS" in too_long.json()["error"]["message"]
    assert "10" in too_long.json()["error"]["message"]

    monkeypatch.setattr(settings, "knowledge_max_total_chars", 15)
    await _entry(client, headers, "a", "x" * 10)
    over_total = await client.post(
        "/knowledge/qa", json={"question": "b", "answer": "y" * 6}, headers=headers
    )
    assert over_total.status_code == 422
    assert "KNOWLEDGE_MAX_TOTAL_CHARS" in over_total.json()["error"]["message"]

    monkeypatch.setattr(settings, "knowledge_max_items", 1)
    over_count = await client.post(
        "/knowledge/entries", json={"title": "c", "text": "z"}, headers=headers
    )
    assert over_count.status_code == 422
    assert "KNOWLEDGE_MAX_ITEMS" in over_count.json()["error"]["message"]


async def test_patch_limits_and_duplicate_title(client):
    headers = await _tenant(client, "Acme")
    first = await _entry(client, headers, "Horário", "9h às 18h")
    dup = await client.post(
        "/knowledge/entries", json={"title": "Horário", "text": "outro"}, headers=headers
    )
    assert dup.status_code == 409
    assert dup.json()["error"]["message"] == "title already exists"
    # The same title as a pair is a different kind: allowed.
    same_as_pair = await client.post(
        "/knowledge/qa", json={"question": "Horário", "answer": "9h"}, headers=headers
    )
    assert same_as_pair.status_code == 201
    other = await _entry(client, headers, "Entrega", "toda a cidade")
    clash = await client.patch(
        f"/knowledge/{other['id']}", json={"title": "Horário"}, headers=headers
    )
    assert clash.status_code == 409
    assert (await client.get(f"/knowledge/{first['id']}", headers=headers)).status_code == 200


async def test_list_pages_with_cursor_and_filters(client, factory):
    headers = await _tenant(client, "Acme")
    ids = [(await _entry(client, headers, f"E{i}", f"texto {i}"))["id"] for i in range(3)]
    await client.post("/knowledge/qa", json={"question": "Q", "answer": "A"}, headers=headers)
    page = await client.get("/knowledge?limit=2", headers=headers)
    assert [i["id"] for i in page.json()] == ids[:2]
    cursor = page.headers["X-Next-Cursor"]
    rest = await client.get(f"/knowledge?limit=2&cursor={cursor}", headers=headers)
    assert [i["kind"] for i in rest.json()] == ["entry", "qa"]
    assert "X-Next-Cursor" not in rest.headers

    entries = await client.get("/knowledge?kind=entry", headers=headers)
    assert [i["id"] for i in entries.json()] == ids
    assert len((await client.get("/knowledge?status=ready", headers=headers)).json()) == 0
    await process_due(factory)
    ready = (await client.get("/knowledge?status=ready", headers=headers)).json()
    assert len(ready) == 4 and all(i["passages"] == 1 for i in ready)


async def test_other_tenant_cannot_touch_or_find(client, factory):
    a = await _tenant(client, "A")
    b = await _tenant(client, "B")
    entry = await _entry(client, a, "Segredo", "Cupom exclusivo XPTO.")
    await process_due(factory)

    assert (await client.get("/knowledge", headers=b)).json() == []
    for response in (
        await client.get(f"/knowledge/{entry['id']}", headers=b),
        await client.patch(f"/knowledge/{entry['id']}", json={"text": "x"}, headers=b),
        await client.delete(f"/knowledge/{entry['id']}", headers=b),
    ):
        assert response.status_code == 404
        assert response.json()["error"]["message"] == "knowledge item not found"
    assert await _search(client, b, "cupom exclusivo") == []
    assert len(await _search(client, a, "cupom exclusivo")) == 1
    # B may use the same title.
    await _entry(client, b, "Segredo", "outro")

    async with factory() as session:
        count = await session.scalar(
            select(func.count()).select_from(KnowledgePassage).where(
                KnowledgePassage.item_id == uuid.UUID(entry["id"])
            )
        )
    assert count == 1
