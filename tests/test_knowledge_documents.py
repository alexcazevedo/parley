"""Knowledge documents: upload (PDF, text, Markdown), extraction in the worker,
failures, truncation, size and type limits, stored file removal, edit, delete."""

import os
import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.knowledge.ingest import process_due
from app.models.knowledge_item import KnowledgeItem
from app.models.knowledge_passage import KnowledgePassage
from tests.test_external_media import _pdf
from tests.test_vision_turn import _png_bytes


@pytest.fixture
def factory(test_engine):
    return async_sessionmaker(bind=test_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def media_root(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_ROOT", str(tmp_path))
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


async def _tenant(client, name: str = "Acme") -> dict:
    response = await client.post(
        "/tenants", json={"name": name}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    return {"X-API-Key": response.json()["api_key"]}


async def _upload(client, headers, data, name, ctype="application/octet-stream", **form):
    return await client.post(
        "/knowledge/documents", data=form, files={"file": (name, data, ctype)}, headers=headers
    )


async def _search(client, headers, query: str) -> list[dict]:
    response = await client.post("/knowledge/search", json={"query": query}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["results"]


async def _get(client, headers, item_id: str) -> dict:
    return (await client.get(f"/knowledge/{item_id}", headers=headers)).json()


async def _row(factory, item_id: str) -> KnowledgeItem:
    async with factory() as session:
        row = await session.scalar(
            select(KnowledgeItem).where(KnowledgeItem.id == uuid.UUID(item_id))
        )
        assert row is not None
        return row


def _files(root) -> list:
    return [p for p in root.rglob("*") if p.is_file()]


async def test_pdf_and_markdown_become_ready_and_searchable(client, factory, media_root):
    headers = await _tenant(client)
    pdf = await _upload(client, headers, _pdf("Garantia de noventa dias"), "garantia.pdf")
    assert pdf.status_code == 201, pdf.text
    assert pdf.json()["kind"] == "document" and pdf.json()["status"] == "processing"
    assert pdf.json()["title"] == "garantia.pdf" and pdf.json()["text"] is None
    md = await _upload(
        client, headers, b"# Frete\n\nEntrega gratis acima de cem reais.", "frete.md"
    )
    assert md.status_code == 201
    assert len(_files(media_root)) == 2

    await process_due(factory)

    for item, query in ((pdf.json(), "garantia noventa"), (md.json(), "frete gratis")):
        got = await _get(client, headers, item["id"])
        assert got["status"] == "ready" and got["passages"] >= 1 and got["chars"] > 0
        assert got["truncated"] is False
        hits = await _search(client, headers, query)
        assert item["id"] in [h["item_id"] for h in hits]
        assert (await _row(factory, item["id"])).source_key is None
    assert _files(media_root) == []


async def test_failures_have_reasons(client, factory, media_root):
    headers = await _tenant(client)
    blank = (await _upload(client, headers, _pdf(None), "scan.pdf")).json()
    binary = (await _upload(client, headers, b"caf\xe9 au lait", "x.txt", "text/plain")).json()
    await process_due(factory)
    got = await _get(client, headers, blank["id"])
    assert got["status"] == "failed"
    assert got["error"] == "no text found in the file (scanned, empty or protected PDF)"
    got = await _get(client, headers, binary["id"])
    assert got["status"] == "failed" and got["error"] == "the file is not UTF-8 text"
    assert _files(media_root) == []

    # Total limit is reached in the worker.
    from app.config import Settings  # noqa: F401

    os.environ["KNOWLEDGE_MAX_TOTAL_CHARS"] = "10"
    get_settings.cache_clear()
    try:
        big = (await _upload(client, headers, b"x" * 50 + b" texto", "b.txt")).json()
        await process_due(factory)
    finally:
        del os.environ["KNOWLEDGE_MAX_TOTAL_CHARS"]
        get_settings.cache_clear()
    got = await _get(client, headers, big["id"])
    assert got["status"] == "failed" and got["error"] == "KNOWLEDGE_MAX_TOTAL_CHARS reached"
    assert _files(media_root) == []


async def test_long_file_is_cut(client, factory, monkeypatch):
    monkeypatch.setenv("PDF_MAX_CHARS", "100")
    get_settings.cache_clear()
    headers = await _tenant(client)
    item = (await _upload(client, headers, ("palavra " * 100).encode(), "l.txt")).json()
    await process_due(factory)
    got = await _get(client, headers, item["id"])
    assert got["status"] == "ready" and got["truncated"] is True and got["chars"] <= 100


async def test_size_and_type_limits(client, monkeypatch):
    headers = await _tenant(client)
    monkeypatch.setenv("MEDIA_MAX_BYTES", "100")
    get_settings.cache_clear()
    assert (await _upload(client, headers, b"a" * 200, "a.txt")).status_code == 413
    monkeypatch.delenv("MEDIA_MAX_BYTES")
    get_settings.cache_clear()
    assert (await _upload(client, headers, _png_bytes(), "a.png", "image/png")).status_code == 415
    assert (await _upload(client, headers, b"hello", "a.docx")).status_code == 415
    ok = await _upload(client, headers, b"hello", "a.bin", "text/plain")
    assert ok.status_code == 201


async def test_same_title_edit_and_delete(client, factory, media_root):
    headers = await _tenant(client)
    first = (
        await _upload(client, headers, b"Prazo de entrega sete dias", "a.txt", title="Guia")
    ).json()
    second = await _upload(client, headers, b"Prazo de entrega dez dias", "b.txt", title="Guia")
    assert second.status_code == 201
    await process_due(factory)

    text = await client.patch(f"/knowledge/{first['id']}", json={"text": "x"}, headers=headers)
    assert text.status_code == 422
    renamed = await client.patch(
        f"/knowledge/{first['id']}", json={"title": "Manual"}, headers=headers
    )
    assert renamed.json()["status"] == "processing"
    await process_due(factory)
    got = await _get(client, headers, first["id"])
    assert got["status"] == "ready" and got["title"] == "Manual"
    hits = await _search(client, headers, "manual sete")
    assert hits[0]["title"] == "Manual"

    queued = (await _upload(client, headers, b"pendente", "c.txt")).json()
    assert len(_files(media_root)) == 1
    assert (await client.delete(f"/knowledge/{queued['id']}", headers=headers)).status_code == 204
    assert _files(media_root) == []

    assert (await client.delete(f"/knowledge/{first['id']}", headers=headers)).status_code == 204
    async with factory() as session:
        rows = await session.scalars(
            select(KnowledgePassage).where(KnowledgePassage.item_id == uuid.UUID(first["id"]))
        )
        assert rows.all() == []


async def test_other_tenant_gets_404(client, factory):
    headers = await _tenant(client, "A")
    other = await _tenant(client, "B")
    item = (await _upload(client, headers, b"segredo", "s.txt")).json()
    await process_due(factory)
    assert (await client.get(f"/knowledge/{item['id']}", headers=other)).status_code == 404
    assert (await client.delete(f"/knowledge/{item['id']}", headers=other)).status_code == 404
    assert (
        await client.patch(f"/knowledge/{item['id']}", json={"title": "x"}, headers=other)
    ).status_code == 404


async def test_pdf_with_more_pages_than_read_is_truncated(client, factory, monkeypatch):
    monkeypatch.setenv("PDF_MAX_PAGES", "1")
    get_settings.cache_clear()
    headers = await _tenant(client)
    item = (await _upload(client, headers, _pdf("Garantia de noventa dias", pages=2), "g.pdf"))
    assert item.status_code == 201, item.text
    await process_due(factory)
    got = await _get(client, headers, item.json()["id"])
    assert got["status"] == "ready" and got["truncated"] is True
