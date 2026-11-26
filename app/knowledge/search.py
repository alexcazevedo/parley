"""Keyword search over a tenant's knowledge passages.

The query gets the passages' treatment (`normalize`), Postgres turns it into
lexemes (`to_tsvector('portuguese', ...)`), and passages matching any lexeme
are ranked by `ts_rank_cd`. A query with no lexemes (only stop words or
punctuation) finds nothing. Hits are cut so their texts total at most
`RESULT_BUDGET_CHARS`. Every query filters on `tenant_id`.

With embeddings on (`app.knowledge.embeddings`) and their table present, the
normalized query is embedded too (the transaction is ended first), the
tenant's passages of the current model with cosine similarity at least
`SEMANTIC_MIN_SIMILARITY` are ranked, and both rankings are merged by
reciprocal rank fusion (`mode` `hybrid`, `score` the fused score). Any
failure on that path gives the keyword result alone (`mode` `keyword`).
"""

import uuid
from dataclasses import dataclass, replace

import structlog
from sqlalchemy import cast, func, select, text
from sqlalchemy.dialects.postgresql import TSQUERY
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import end_transaction
from app.knowledge import embeddings
from app.knowledge.text import normalize
from app.models.knowledge_item import KnowledgeItem, KnowledgeItemKind
from app.models.knowledge_passage import KnowledgePassage

MAX_RESULTS = 4
RESULT_BUDGET_CHARS = 3000
TS_CONFIG = "portuguese"
# Reciprocal rank fusion constant: score = sum of 1 / (RRF_K + rank).
RRF_K = 60

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class Hit:
    passage_id: uuid.UUID
    item_id: uuid.UUID
    kind: KnowledgeItemKind
    title: str
    text: str
    score: float
    keyword_rank: int | None
    semantic_rank: int | None


@dataclass(frozen=True)
class SearchResult:
    mode: str
    hits: list[Hit]


def _quote(lexeme: str) -> str:
    return "'" + lexeme.replace("\\", "\\\\").replace("'", "''") + "'"


async def _lexemes(session: AsyncSession, query: str) -> list[str]:
    rows = await session.scalars(
        select(func.unnest(func.tsvector_to_array(func.to_tsvector(TS_CONFIG, normalize(query)))))
    )
    return [lexeme for lexeme in rows.all() if lexeme]


def _passage_text(heading: str, content: str) -> str:
    return f"{heading}\n{content}" if heading else content


def _budget(hits: list[Hit]) -> list[Hit]:
    """Cut so the texts total at most `RESULT_BUDGET_CHARS` (the first one is shortened)."""
    kept: list[Hit] = []
    left = RESULT_BUDGET_CHARS
    for hit in hits:
        text_ = hit.text
        if len(text_) > left:
            if kept:
                break
            text_ = text_[:left]
        left -= len(text_)
        kept.append(replace(hit, text=text_))
    return kept


async def _keyword(
    session: AsyncSession, tenant_id: uuid.UUID, query: str, limit: int
) -> list[Hit]:
    lexemes = await _lexemes(session, query)
    if not lexemes:
        return []
    # Lexemes are already stemmed: cast to tsquery as they are (no re-parsing).
    tsquery = cast(" | ".join(_quote(lexeme) for lexeme in lexemes), TSQUERY)
    rank = func.ts_rank_cd(KnowledgePassage.search_vector, tsquery).label("rank")
    rows = await session.execute(
        select(
            KnowledgePassage.id,
            KnowledgePassage.item_id,
            KnowledgePassage.heading,
            KnowledgePassage.content,
            KnowledgeItem.kind,
            KnowledgeItem.title,
            rank,
        )
        .join(KnowledgeItem, KnowledgeItem.id == KnowledgePassage.item_id)
        .where(
            KnowledgePassage.tenant_id == tenant_id,
            KnowledgeItem.tenant_id == tenant_id,
            KnowledgePassage.search_vector.op("@@")(tsquery),
        )
        .order_by(rank.desc(), KnowledgePassage.item_id, KnowledgePassage.position)
        .limit(limit)
    )
    return [
        Hit(
            passage_id=row.id,
            item_id=row.item_id,
            kind=row.kind,
            title=row.title,
            text=_passage_text(row.heading, row.content),
            score=float(row.rank),
            keyword_rank=position,
            semantic_rank=None,
        )
        for position, row in enumerate(rows.all(), start=1)
    ]


_SEMANTIC_SQL = text(
    """
    SELECT p.id, p.item_id, p.heading, p.content, i.kind, i.title,
           1 - (e.embedding <=> CAST(:vector AS vector)) AS similarity
    FROM knowledge_embeddings e
    JOIN knowledge_passages p ON p.id = e.passage_id
    JOIN knowledge_items i ON i.id = p.item_id
    WHERE e.tenant_id = :tenant_id AND p.tenant_id = :tenant_id AND i.tenant_id = :tenant_id
      AND e.model = :model
      AND 1 - (e.embedding <=> CAST(:vector AS vector)) >= :min_similarity
    ORDER BY e.embedding <=> CAST(:vector AS vector), p.item_id, p.position
    LIMIT :limit
    """
)


async def _semantic(
    session: AsyncSession, tenant_id: uuid.UUID, query: str, limit: int
) -> list[Hit] | None:
    """Nearest passages by meaning; `None` when this path is off or failed."""
    normalized = normalize(query)
    if not embeddings.enabled() or not normalized:
        return None
    try:
        if not await embeddings.storage_ready(session):
            return None
        await end_transaction(session)
        [vector] = await embeddings.embed([normalized])
        async with session.begin_nested():
            rows = (
                await session.execute(
                    _SEMANTIC_SQL,
                    {
                        "vector": embeddings.vector_literal(vector),
                        "tenant_id": tenant_id,
                        "model": get_settings().embeddings_model,
                        "min_similarity": embeddings.SEMANTIC_MIN_SIMILARITY,
                        "limit": limit,
                    },
                )
            ).all()
    except Exception as exc:
        logger.warning(
            "knowledge.semantic_search_failed", tenant_id=str(tenant_id), error=str(exc)[:300]
        )
        return None
    return [
        Hit(
            passage_id=row.id,
            item_id=row.item_id,
            kind=KnowledgeItemKind(row.kind),
            title=row.title,
            text=_passage_text(row.heading, row.content),
            score=float(row.similarity),
            keyword_rank=None,
            semantic_rank=position,
        )
        for position, row in enumerate(rows, start=1)
    ]


def _fuse(keyword: list[Hit], semantic: list[Hit], limit: int) -> list[Hit]:
    merged: dict[uuid.UUID, Hit] = {}
    scores: dict[uuid.UUID, float] = {}
    for hit in keyword:
        merged[hit.passage_id] = hit
        scores[hit.passage_id] = 1 / (RRF_K + (hit.keyword_rank or 0))
    for hit in semantic:
        rank = hit.semantic_rank or 0
        if hit.passage_id in merged:
            merged[hit.passage_id] = replace(merged[hit.passage_id], semantic_rank=rank)
        else:
            merged[hit.passage_id] = replace(hit, keyword_rank=None)
        scores[hit.passage_id] = scores.get(hit.passage_id, 0.0) + 1 / (RRF_K + rank)
    ordered = sorted(
        merged.values(),
        key=lambda hit: (
            -scores[hit.passage_id],
            hit.keyword_rank or limit + 1,
            hit.semantic_rank or limit + 1,
        ),
    )
    return [replace(hit, score=scores[hit.passage_id]) for hit in ordered[:limit]]


async def search(
    session: AsyncSession, tenant_id: uuid.UUID, query: str, *, limit: int = MAX_RESULTS
) -> SearchResult:
    keyword = await _keyword(session, tenant_id, query, limit)
    semantic = await _semantic(session, tenant_id, query, limit)
    if semantic is None:
        return SearchResult(mode="keyword", hits=_budget(keyword))
    return SearchResult(mode="hybrid", hits=_budget(_fuse(keyword, semantic, limit)))
