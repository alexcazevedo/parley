"""Embeddings of knowledge passages, for search by meaning.

Off unless `EMBEDDINGS_BASE_URL` is set. The endpoint is OpenAI-compatible:
`POST {EMBEDDINGS_BASE_URL}/embeddings` with `{"model", "input": [...]}` and a
bearer `EMBEDDINGS_API_KEY`.

Storage is optional so plain Postgres keeps working: the table
`knowledge_embeddings` lives outside the ORM and is created by raw SQL
(`ensure_storage`) only where the `vector` extension is available (migration
0023 and `python -m app.knowledge.reindex`). The vector column has no fixed
dimension; every query filters on `model`, so vectors of another model are
ignored until re-indexed. Vectors travel as text literals (`'[1,2,3]'`).
"""

import math
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import httpx
import structlog
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.config import get_settings
from app.http_client import shared_client
from app.http_retry import send_with_retry

logger = structlog.get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

SEMANTIC_MIN_SIMILARITY = 0.25
TIMEOUT_SECONDS = 20.0
# Texts sent in one embeddings request.
BATCH_SIZE = 64

STORAGE_SQL = (
    """
    CREATE TABLE IF NOT EXISTS knowledge_embeddings (
        passage_id uuid PRIMARY KEY REFERENCES knowledge_passages(id) ON DELETE CASCADE,
        tenant_id uuid NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
        model text NOT NULL,
        embedding vector NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_knowledge_embeddings_tenant_model "
    "ON knowledge_embeddings (tenant_id, model)",
)


class EmbeddingsError(Exception):
    """The embeddings endpoint failed or answered something unusable."""


def enabled() -> bool:
    return bool(get_settings().embeddings_base_url.strip())


def vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


async def embed(texts: list[str]) -> list[list[float]]:
    """One vector per text, in order. Raises `EmbeddingsError`. Hold no
    transaction across this call."""
    if not texts:
        return []
    settings = get_settings()
    base_url = settings.embeddings_base_url.strip().rstrip("/")
    if not base_url:
        raise EmbeddingsError("EMBEDDINGS_BASE_URL is not set")
    payload = {"model": settings.embeddings_model, "input": texts}
    try:
        response = await send_with_retry(
            lambda: shared_client().post(
                f"{base_url}/embeddings",
                json=payload,
                timeout=TIMEOUT_SECONDS,
                headers={
                    "Authorization": f"Bearer {settings.embeddings_api_key}",
                    "Content-Type": "application/json",
                },
            )
        )
        response.raise_for_status()
        body: Any = response.json()
    except httpx.HTTPStatusError as exc:
        raise EmbeddingsError(f"{exc.response.status_code} from {base_url}") from exc
    except (httpx.HTTPError, ValueError) as exc:
        raise EmbeddingsError(f"error talking to {base_url}: {type(exc).__name__}") from exc
    try:
        data = sorted(body["data"], key=lambda row: int(row.get("index", 0)))
        vectors = [[float(x) for x in row["embedding"]] for row in data]
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise EmbeddingsError("unexpected embeddings body") from exc
    if len(vectors) != len(texts):
        raise EmbeddingsError(f"{len(vectors)} vectors for {len(texts)} texts")
    for vector in vectors:
        if not vector or not all(math.isfinite(x) for x in vector):
            raise EmbeddingsError("empty or non-finite vector")
        if not any(vector):
            raise EmbeddingsError("zero vector")
    return vectors


async def ensure_storage(conn: AsyncConnection) -> bool:
    """Create the extension and the table when possible; `False` when the
    `vector` extension is unavailable (or may not be created here)."""
    available = await conn.scalar(
        text("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
    )
    if not available:
        return False
    try:
        async with conn.begin_nested():
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    except DBAPIError:
        logger.warning("knowledge.vector_extension_unavailable")
        return False
    for statement in STORAGE_SQL:
        await conn.execute(text(statement))
    return True


async def storage_ready(session: AsyncSession) -> bool:
    return bool(
        await session.scalar(text("SELECT to_regclass('knowledge_embeddings') IS NOT NULL"))
    )


_PENDING_SQL = """
    SELECT p.id, p.tenant_id, p.search_text
    FROM knowledge_passages p
    WHERE p.id > :after
      {scope}
      {missing}
    ORDER BY p.id
    LIMIT :batch
"""
_MISSING = (
    "AND NOT EXISTS (SELECT 1 FROM knowledge_embeddings e "
    "WHERE e.passage_id = p.id AND e.model = :model)"
)
_WRITE_SQL = """
    INSERT INTO knowledge_embeddings (passage_id, tenant_id, model, embedding)
    SELECT p.id, p.tenant_id, :model, CAST(:embedding AS vector)
    FROM knowledge_passages p
    WHERE p.id = :passage_id AND p.tenant_id = :tenant_id
    ON CONFLICT (passage_id) DO UPDATE
    SET model = EXCLUDED.model, embedding = EXCLUDED.embedding
"""


async def embed_pending(
    session_factory: SessionFactory,
    *,
    tenant_id: uuid.UUID | None = None,
    item_id: uuid.UUID | None = None,
    everything: bool = False,
) -> int:
    """Embed passages with no vector of the current model (`everything`: all).

    With `tenant_id` and `item_id`, only that item's passages; without, every
    tenant's (the operator's re-index command). Reads and writes are short
    transactions; none is open during the HTTP call. Returns how many were
    written. Raises `EmbeddingsError`.
    """
    model = get_settings().embeddings_model
    scope = ""
    params: dict[str, Any] = {"model": model, "batch": BATCH_SIZE}
    if tenant_id is not None:
        scope += " AND p.tenant_id = :tenant_id"
        params["tenant_id"] = tenant_id
    if item_id is not None:
        scope += " AND p.item_id = :item_id"
        params["item_id"] = item_id
    query = text(_PENDING_SQL.format(scope=scope, missing="" if everything else _MISSING))
    after = uuid.UUID(int=0)
    written = 0
    while True:
        async with session_factory() as session:
            rows = (await session.execute(query, {**params, "after": after})).all()
            await session.rollback()
        if not rows:
            return written
        after = rows[-1].id
        vectors = await embed([row.search_text for row in rows])
        async with session_factory() as session:
            for row, vector in zip(rows, vectors, strict=True):
                result = await session.execute(
                    text(_WRITE_SQL),
                    {
                        "passage_id": row.id,
                        "tenant_id": row.tenant_id,
                        "model": model,
                        "embedding": vector_literal(vector),
                    },
                )
                written += result.rowcount  # type: ignore[attr-defined]
            await session.commit()
        if len(rows) < BATCH_SIZE:
            return written
