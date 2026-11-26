"""The tenant's knowledge base: entries, question-and-answer pairs, and a search to try.

Create and every edit of title or text set the item to `processing`; the
worker (app/knowledge/ingest.py) splits it into passages and sets `ready`.
Limits (`KNOWLEDGE_MAX_ITEMS`, `KNOWLEDGE_MAX_ITEM_CHARS`,
`KNOWLEDGE_MAX_TOTAL_CHARS`) are refused with 422 naming the setting.
"""

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field, StringConstraints
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.api.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, set_next_cursor
from app.config import get_settings
from app.knowledge import search as knowledge_search
from app.media.sniff import sniff_mime
from app.media.storage import get_media_storage
from app.models.knowledge_item import KnowledgeItem, KnowledgeItemKind, KnowledgeItemStatus
from app.models.tenant import Tenant
from app.repositories.base import parse_after
from app.repositories.knowledge import KnowledgeItemsRepository

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class EntryCreate(BaseModel):
    title: Title
    text: Text


class QACreate(BaseModel):
    question: Title
    answer: Text


class ItemUpdate(BaseModel):
    title: Title | None = None
    text: Text | None = None


class ItemOut(BaseModel):
    id: uuid.UUID
    kind: KnowledgeItemKind
    title: str
    text: str | None
    filename: str | None
    status: KnowledgeItemStatus
    error: str | None
    chars: int
    passages: int
    truncated: bool
    created_at: datetime
    updated_at: datetime


class SearchIn(BaseModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    limit: int = Field(default=knowledge_search.MAX_RESULTS, ge=1, le=20)


class HitOut(BaseModel):
    passage_id: uuid.UUID
    item_id: uuid.UUID
    kind: KnowledgeItemKind
    title: str
    text: str
    score: float
    keyword_rank: int | None
    semantic_rank: int | None


class SearchOut(BaseModel):
    mode: str
    results: list[HitOut]


def _repo(db: AsyncSession) -> KnowledgeItemsRepository:
    return KnowledgeItemsRepository(db)


def _out(item: KnowledgeItem, passages: int) -> ItemOut:
    return ItemOut(
        id=item.id,
        kind=item.kind,
        title=item.title,
        text=None if item.kind == KnowledgeItemKind.DOCUMENT else item.body,
        filename=item.filename,
        status=item.status,
        error=item.error,
        chars=item.chars,
        passages=passages,
        truncated=item.truncated,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


async def _item_out(db: AsyncSession, item: KnowledgeItem) -> ItemOut:
    counts = await _repo(db).passage_counts(item.tenant_id, [item.id])
    return _out(item, counts.get(item.id, 0))


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="knowledge item not found")


def _duplicate() -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail="title already exists")


def _over(setting: str, value: int) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail=f"knowledge limit exceeded: {setting} is {value}",
    )


async def _check_text(db: AsyncSession, tenant_id: uuid.UUID, chars: int, freed: int) -> None:
    """Refuse a text of `chars` characters replacing `freed` stored ones.

    Read-then-write: two concurrent creates may pass the total together (mvp
    shortcut; the limits are a guard, not an accounting).
    """
    settings = get_settings()
    if chars > settings.knowledge_max_item_chars:
        raise _over("KNOWLEDGE_MAX_ITEM_CHARS", settings.knowledge_max_item_chars)
    total = await _repo(db).total_chars(tenant_id)
    if total - freed + chars > settings.knowledge_max_total_chars:
        raise _over("KNOWLEDGE_MAX_TOTAL_CHARS", settings.knowledge_max_total_chars)


async def _create(
    db: AsyncSession, tenant: Tenant, kind: KnowledgeItemKind, title: str, body: str
) -> ItemOut:
    settings = get_settings()
    if await _repo(db).count(tenant.id) >= settings.knowledge_max_items:
        raise _over("KNOWLEDGE_MAX_ITEMS", settings.knowledge_max_items)
    await _check_text(db, tenant.id, len(body), 0)
    try:
        item = await _repo(db).create(
            tenant.id,
            kind=kind,
            title=title,
            body=body,
            status=KnowledgeItemStatus.PROCESSING,
            chars=len(body),
            truncated=False,
        )
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _duplicate() from exc
    await db.refresh(item)
    return _out(item, 0)


@router.post("/entries", response_model=ItemOut, status_code=status.HTTP_201_CREATED)
async def create_entry(
    payload: EntryCreate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ItemOut:
    return await _create(db, tenant, KnowledgeItemKind.ENTRY, payload.title, payload.text)


@router.post("/qa", response_model=ItemOut, status_code=status.HTTP_201_CREATED)
async def create_qa(
    payload: QACreate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ItemOut:
    return await _create(db, tenant, KnowledgeItemKind.QA, payload.question, payload.answer)


_CHUNK_SIZE = 64 * 1024


def _text_mime(filename: str, content_type: str | None) -> str | None:
    name = filename.lower()
    ctype = (content_type or "").split(";")[0].strip().lower()
    if name.endswith(".md") or ctype == "text/markdown":
        return "text/markdown"
    if name.endswith(".txt") or ctype == "text/plain":
        return "text/plain"
    return None


@router.post("/documents", response_model=ItemOut, status_code=status.HTTP_201_CREATED)
async def create_document(
    file: UploadFile = File(...),
    title: str | None = Form(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ItemOut:
    """Checks and stores the file; the worker extracts the text (`processing`)."""
    settings = get_settings()
    max_bytes = settings.media_max_bytes
    buffer = bytearray()
    while chunk := await file.read(_CHUNK_SIZE):
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="file exceeds max size",
            )
    data = bytes(buffer)
    filename = (file.filename or "").strip() or "document"
    sniffed = sniff_mime(data)
    mime: str | None
    if sniffed == "application/pdf":
        mime = sniffed
    elif sniffed is None:
        mime = _text_mime(filename, file.content_type)
    else:
        mime = None
    if mime is None:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="unsupported file: send a PDF, a .txt or a .md file",
        )
    if await _repo(db).count(tenant.id) >= settings.knowledge_max_items:
        raise _over("KNOWLEDGE_MAX_ITEMS", settings.knowledge_max_items)
    name = (title or "").strip() or filename
    storage = get_media_storage()
    key = await storage.put(tenant.id, data, mime)
    try:
        item = await _repo(db).create(
            tenant.id,
            kind=KnowledgeItemKind.DOCUMENT,
            title=name[:300],
            body=None,
            status=KnowledgeItemStatus.PROCESSING,
            chars=0,
            truncated=False,
            filename=filename[:255],
            source_key=key,
            source_mime=mime,
        )
        await db.commit()
    except Exception:
        await db.rollback()
        await storage.delete(key)
        raise
    await db.refresh(item)
    return _out(item, 0)


@router.get("", response_model=list[ItemOut])
async def list_items(
    response: Response,
    kind: KnowledgeItemKind | None = Query(default=None),
    item_status: KnowledgeItemStatus | None = Query(default=None, alias="status"),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[ItemOut]:
    filters: dict = {}
    if kind is not None:
        filters["kind"] = kind
    if item_status is not None:
        filters["status"] = item_status
    after = parse_after(decode_cursor(cursor))
    repo = _repo(db)
    rows = await repo.list_page(tenant.id, limit=limit + 1, after=after, **filters)
    more = len(rows) > limit
    rows = rows[:limit]
    if more:
        set_next_cursor(
            response, {"created_at": rows[-1].created_at.isoformat(), "id": str(rows[-1].id)}
        )
    counts = await repo.passage_counts(tenant.id, [r.id for r in rows])
    return [_out(r, counts.get(r.id, 0)) for r in rows]


@router.post("/search", response_model=SearchOut)
async def search_knowledge(
    payload: SearchIn,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> SearchOut:
    result = await knowledge_search.search(db, tenant.id, payload.query, limit=payload.limit)
    return SearchOut(
        mode=result.mode,
        results=[
            HitOut(
                passage_id=h.passage_id,
                item_id=h.item_id,
                kind=h.kind,
                title=h.title,
                text=h.text,
                score=h.score,
                keyword_rank=h.keyword_rank,
                semantic_rank=h.semantic_rank,
            )
            for h in result.hits
        ],
    )


@router.get("/{item_id}", response_model=ItemOut)
async def get_item(
    item_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ItemOut:
    item = await _repo(db).get(tenant.id, item_id)
    if item is None:
        raise _not_found()
    return await _item_out(db, item)


@router.patch("/{item_id}", response_model=ItemOut)
async def update_item(
    item_id: uuid.UUID,
    payload: ItemUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ItemOut:
    repo = _repo(db)
    item = await repo.get(tenant.id, item_id)
    if item is None:
        raise _not_found()
    changes: dict = {}
    if payload.title is not None:
        changes["title"] = payload.title
    if payload.text is not None:
        if item.kind == KnowledgeItemKind.DOCUMENT:
            raise HTTPException(
                status_code=422,
                detail="the text of a document cannot be changed; upload a new document",
            )
        await _check_text(db, tenant.id, len(payload.text), item.chars)
        changes["body"] = payload.text
        changes["chars"] = len(payload.text)
    if changes:
        # A new `updated_at` and no lease: a worker splitting the old text
        # does not finish it (app/knowledge/ingest.py), the next pass does.
        changes |= {
            "status": KnowledgeItemStatus.PROCESSING,
            "error": None,
            "claimed_until": None,
            "updated_at": datetime.now(UTC),
        }
        try:
            await repo.update(tenant.id, item_id, **changes)
            await db.commit()
        except IntegrityError as exc:
            await db.rollback()
            raise _duplicate() from exc
        await db.refresh(item)
    return await _item_out(db, item)


@router.delete("/{item_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_item(
    item_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> None:
    repo = _repo(db)
    item = await repo.get(tenant.id, item_id)
    if item is None:
        raise _not_found()
    source_key = item.source_key
    await repo.delete(tenant.id, item_id)
    await db.commit()
    if source_key:
        await get_media_storage().delete(source_key)
