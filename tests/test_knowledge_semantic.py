"""Semantic knowledge search: embeddings after ingest, hybrid ranking, fallbacks,
model change and re-index, tenant isolation, cascade on delete, migration.

The embeddings endpoint is mocked with `respx`: a toy model maps a few words
to shared "meaning" axes, so a query can match a passage with no word in
common. Tests that need the `vector` extension skip when it is unavailable.
"""

import hashlib
import json
import math
import os
import re
import uuid

import httpx
import pytest
import pytest_asyncio
import respx
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.knowledge.embeddings import ensure_storage
from app.knowledge.ingest import process_due
from app.knowledge.reindex import ReindexError, reindex
from app.models.knowledge_passage import KnowledgePassage

BASE_URL = "https://embed.example.com/v1"
DIMENSIONS = 64
# Words sharing an axis "mean" the same thing.
CONCEPTS = {
    "frete": 0, "envio": 0, "entrega": 0, "remessa": 0, "correios": 0, "despacho": 0,
    "pagamento": 1, "pix": 1, "boleto": 1, "cartao": 1,
    "horario": 2, "abrimos": 2, "fechamos": 2,
}
_WORDS = re.compile(r"\w+")


def _vector(text_: str) -> list[float]:
    vector = [0.0] * DIMENSIONS
    for word in _WORDS.findall(text_.lower()):
        if word in CONCEPTS:
            vector[CONCEPTS[word]] += 1.0
        else:
            digest = hashlib.sha256(word.encode()).digest()
            vector[8 + digest[0] % (DIMENSIONS - 8)] += 0.2
    if not any(vector):
        vector[DIMENSIONS - 1] = 1.0
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


def _answer(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    assert request.headers["authorization"] == "Bearer embed-key"
    return httpx.Response(
        200,
        json={"data": [{"index": i, "embedding": _vector(t)} for i, t in enumerate(body["input"])]},
    )


@pytest.fixture
def factory(test_engine):
    return async_sessionmaker(bind=test_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def storage(test_engine):
    async with test_engine.begin() as conn:
        ok = await ensure_storage(conn)
    if not ok:
        pytest.skip("Postgres `vector` extension unavailable (use pgvector/pgvector:pg16)")


@pytest.fixture
def embeddings_on(monkeypatch):
    monkeypatch.setenv("EMBEDDINGS_BASE_URL", BASE_URL)
    monkeypatch.setenv("EMBEDDINGS_API_KEY", "embed-key")
    monkeypatch.setenv("EMBEDDINGS_MODEL", "concept-1")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def endpoint():
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{BASE_URL}/embeddings").mock(side_effect=_answer)
        yield route


async def _tenant(client, name: str = "Acme") -> dict:
    response = await client.post(
        "/tenants", json={"name": name}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    return {"X-API-Key": response.json()["api_key"]}


async def _entry(client, headers, title: str, text_: str) -> str:
    response = await client.post(
        "/knowledge/entries", json={"title": title, "text": text_}, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def _search(client, headers, query: str) -> dict:
    response = await client.post("/knowledge/search", json={"query": query}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


async def _vectors(factory, item_id: str) -> int:
    async with factory() as session:
        ids = list(
            (
                await session.scalars(
                    select(KnowledgePassage.id).where(
                        KnowledgePassage.item_id == uuid.UUID(item_id)
                    )
                )
            ).all()
        )
        count = await session.scalar(
            text("SELECT count(*) FROM knowledge_embeddings WHERE passage_id = ANY(:ids)"),
            {"ids": ids},
        )
        await session.rollback()
        return int(count or 0)


async def test_meaning_without_shared_words_is_hybrid(client, factory, storage, embeddings_on,
                                                      endpoint):
    headers = await _tenant(client)
    frete = await _entry(client, headers, "Frete", "Frete grátis acima de cem reais.")
    await _entry(client, headers, "Horário", "Abrimos às 9h.")
    await process_due(factory)
    assert await _vectors(factory, frete) == 1

    body = await _search(client, headers, "remessa")
    assert body["mode"] == "hybrid"
    assert [h["item_id"] for h in body["results"]] == [frete]
    hit = body["results"][0]
    assert hit["keyword_rank"] is None and hit["semantic_rank"] == 1 and hit["score"] > 0


async def test_found_by_both_ranks_first(client, factory, storage, embeddings_on, endpoint):
    headers = await _tenant(client)
    frete = await _entry(client, headers, "Frete", "Frete grátis acima de cem reais.")
    correios = await _entry(client, headers, "Correios", "Envio pelos correios.")
    await process_due(factory)

    body = await _search(client, headers, "frete remessa")
    assert body["mode"] == "hybrid"
    results = body["results"]
    assert [h["item_id"] for h in results] == [frete, correios]
    assert results[0]["keyword_rank"] == 1 and results[0]["semantic_rank"] == 2
    assert results[1]["keyword_rank"] is None and results[1]["semantic_rank"] == 1


async def test_endpoint_error_falls_back_to_keyword(client, factory, storage, embeddings_on,
                                                    endpoint):
    endpoint.mock(side_effect=None, return_value=httpx.Response(500, json={"error": "down"}))
    headers = await _tenant(client)
    frete = await _entry(client, headers, "Frete", "Frete grátis acima de cem reais.")
    await process_due(factory)
    got = (await client.get(f"/knowledge/{frete}", headers=headers)).json()
    assert got["status"] == "ready"
    assert await _vectors(factory, frete) == 0

    body = await _search(client, headers, "frete")
    assert body["mode"] == "keyword"
    assert [h["item_id"] for h in body["results"]] == [frete]
    assert endpoint.call_count > 0


async def test_endpoint_unset_makes_no_call(client, factory, test_engine, storage, endpoint,
                                            monkeypatch):
    monkeypatch.delenv("EMBEDDINGS_BASE_URL", raising=False)
    get_settings.cache_clear()
    headers = await _tenant(client)
    frete = await _entry(client, headers, "Frete", "Frete grátis acima de cem reais.")
    await process_due(factory)
    body = await _search(client, headers, "frete")
    assert body["mode"] == "keyword" and [h["item_id"] for h in body["results"]] == [frete]
    assert (await _search(client, headers, "remessa"))["results"] == []
    assert endpoint.call_count == 0
    with pytest.raises(ReindexError):
        await reindex(test_engine)


async def test_model_change_needs_reindex(client, factory, test_engine, storage, embeddings_on,
                                          endpoint, monkeypatch):
    headers = await _tenant(client)
    frete = await _entry(client, headers, "Frete", "Frete grátis acima de cem reais.")
    await process_due(factory)
    assert [h["item_id"] for h in (await _search(client, headers, "remessa"))["results"]] == [
        frete
    ]

    monkeypatch.setenv("EMBEDDINGS_MODEL", "concept-2")
    get_settings.cache_clear()
    body = await _search(client, headers, "remessa")
    assert body["mode"] == "hybrid" and body["results"] == []

    assert await reindex(test_engine) >= 1
    assert await reindex(test_engine) == 0
    body = await _search(client, headers, "remessa")
    assert [h["item_id"] for h in body["results"]] == [frete]
    assert await reindex(test_engine, everything=True) >= 1


async def test_other_tenant_never_gets_the_passage(client, factory, storage, embeddings_on,
                                                   endpoint):
    acme = await _tenant(client, "Acme")
    await _entry(client, acme, "Frete", "Frete grátis acima de cem reais.")
    other = await _tenant(client, "Other")
    await _entry(client, other, "Pix", "Aceitamos pix.")
    await process_due(factory)
    for query in ("remessa", "frete grátis acima de cem reais"):
        body = await _search(client, other, query)
        assert body["mode"] == "hybrid" and body["results"] == [], query


async def test_delete_removes_vectors(client, factory, storage, embeddings_on, endpoint):
    headers = await _tenant(client)
    frete = await _entry(client, headers, "Frete", "Frete grátis acima de cem reais.")
    await process_due(factory)
    async with factory() as session:
        ids = list(
            (
                await session.scalars(
                    select(KnowledgePassage.id).where(KnowledgePassage.item_id == uuid.UUID(frete))
                )
            ).all()
        )
        await session.rollback()
    assert ids and await _vectors(factory, frete) == 1
    assert (await client.delete(f"/knowledge/{frete}", headers=headers)).status_code == 204
    async with factory() as session:
        left = await session.scalar(
            text("SELECT count(*) FROM knowledge_embeddings WHERE passage_id = ANY(:ids)"),
            {"ids": ids},
        )
        await session.rollback()
    assert left == 0


async def test_migration_creates_and_drops_the_table(storage):
    import asyncpg

    from tests.conftest import _ASYNCPG_ADMIN_DSN
    from tests.test_migration_open_conversation import _alembic

    db_name = f"parley_mig_{uuid.uuid4().hex[:12]}"
    admin = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/postgres")
    await admin.execute(f'CREATE DATABASE "{db_name}"')
    database_url = (
        _ASYNCPG_ADMIN_DSN.replace("postgresql://", "postgresql+asyncpg://") + f"/{db_name}"
    )
    query = "SELECT to_regclass('knowledge_embeddings') IS NOT NULL"
    try:
        _alembic(database_url, "upgrade", "head")
        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            assert await conn.fetchval(query) is True
        finally:
            await conn.close()
        _alembic(database_url, "downgrade", "0022")
        conn = await asyncpg.connect(f"{_ASYNCPG_ADMIN_DSN}/{db_name}")
        try:
            assert await conn.fetchval(query) is False
        finally:
            await conn.close()
    finally:
        await admin.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')
        await admin.close()
