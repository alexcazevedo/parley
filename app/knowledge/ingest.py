"""Worker pass that splits knowledge items into passages.

`process_due` is one pass of `knowledge_ingest_loop` (`app/workers/main.py`).
It takes items in `processing` one at a time.

Claim: an item in `processing` whose lease (`claimed_until`) is empty or past
is locked `FOR UPDATE SKIP LOCKED`, its lease moved `CLAIM_LEASE_SECONDS`
ahead, and the transaction commits. No transaction is held while the text is
extracted and split. A worker that dies leaves the lease, and the item is
taken again after it.

Finish: in one transaction the item's passages are replaced and the item set
`ready` (or `failed` with the reason). The update is conditional on the
`updated_at` seen at claim time: an item edited meanwhile (the edit sets a new
`updated_at` and clears the lease) is left in `processing` for the next pass,
and its old passages stay. Until then the old passages stay searchable.

After an item became ready, `after_passages` embeds its passages when
embeddings are on (`app.knowledge.embeddings`); a failure there is logged and
leaves the item `ready` (keyword search still works).
"""

import asyncio
import io
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from pypdf import PdfReader
from sqlalchemy import delete, func, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.knowledge import embeddings
from app.knowledge.text import normalize, split_passages
from app.media.pdf import extract_pdf_text
from app.media.storage import get_media_storage
from app.models.knowledge_item import KnowledgeItem, KnowledgeItemKind, KnowledgeItemStatus
from app.models.knowledge_passage import KnowledgePassage

logger = structlog.get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

CLAIM_LEASE_SECONDS = 120
# Items one pass processes at most.
MAX_ITEMS_PER_PASS = 50
_MAX_ERROR_CHARS = 500


@dataclass(frozen=True)
class _Claimed:
    id: uuid.UUID
    tenant_id: uuid.UUID
    title: str
    body: str | None
    updated_at: datetime
    item: KnowledgeItem


async def _claim(session_factory: SessionFactory, now: datetime) -> _Claimed | None:
    async with session_factory() as session:
        item = await session.scalar(
            select(KnowledgeItem)
            .where(
                KnowledgeItem.status == KnowledgeItemStatus.PROCESSING,
                or_(KnowledgeItem.claimed_until.is_(None), KnowledgeItem.claimed_until <= now),
            )
            .order_by(KnowledgeItem.updated_at, KnowledgeItem.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if item is None:
            await session.rollback()
            return None
        item.claimed_until = now + timedelta(seconds=CLAIM_LEASE_SECONDS)
        claimed = _Claimed(item.id, item.tenant_id, item.title, item.body, item.updated_at, item)
        await session.commit()
        return claimed


class ExtractionFailed(Exception):
    """The file gave no usable text; the message is the reason shown to the tenant."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


NO_TEXT = "no text found in the file (scanned, empty or protected PDF)"


def _read_pdf(data: bytes, max_pages: int, max_chars: int) -> tuple[str, int | None]:
    """The PDF's text and its page count (`None` when it cannot be counted). Blocking."""
    text = extract_pdf_text(data, max_pages, max_chars)
    try:
        pages: int | None = len(PdfReader(io.BytesIO(data)).pages)
    except Exception:  # pypdf raises many types on bad input
        pages = None
    return text, pages


async def extract_text(item: KnowledgeItem) -> tuple[str, bool]:
    """The item's text and whether it was truncated. Entries and pairs: `body`.

    A document with a stored file (`source_key`) is read from it: a PDF through
    `extract_pdf_text`, text decoded as UTF-8, both cut at `PDF_MAX_CHARS`. A
    document without one (its title was edited) already has its text in `body`.
    """
    if item.kind != KnowledgeItemKind.DOCUMENT:
        return item.body or "", False
    if not item.source_key:
        if not item.body:
            raise ExtractionFailed(NO_TEXT)
        return item.body, item.truncated
    settings = get_settings()
    limit = settings.pdf_max_chars
    data = await get_media_storage().get(item.source_key)
    more_pages = False
    if item.source_mime == "application/pdf":
        # One character over the limit tells whether the text was cut.
        text, pages = await asyncio.to_thread(_read_pdf, data, settings.pdf_max_pages, limit + 1)
        more_pages = pages is not None and pages > settings.pdf_max_pages
    else:
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ExtractionFailed("the file is not UTF-8 text") from exc
        text = text.replace("\x00", "")
    truncated = len(text) > limit or more_pages
    text = text[:limit].strip()
    if not text:
        raise ExtractionFailed(NO_TEXT)
    return text, truncated


async def _check_total(session_factory: SessionFactory, claimed: "_Claimed", chars: int) -> None:
    async with session_factory() as session:
        others = await session.scalar(
            select(func.coalesce(func.sum(KnowledgeItem.chars), 0)).where(
                KnowledgeItem.tenant_id == claimed.tenant_id, KnowledgeItem.id != claimed.id
            )
        )
        await session.rollback()
    if int(others or 0) + chars > get_settings().knowledge_max_total_chars:
        raise ExtractionFailed("KNOWLEDGE_MAX_TOTAL_CHARS reached")


async def after_passages(
    session_factory: SessionFactory, tenant_id: uuid.UUID, item_id: uuid.UUID
) -> None:
    """Hook run after an item became ready: embed its passages when embeddings
    are on and their table exists. Failures are logged, never raised."""
    if not embeddings.enabled():
        return
    try:
        async with session_factory() as session:
            ready = await embeddings.storage_ready(session)
            await session.rollback()
        if not ready:
            return
        await embeddings.embed_pending(session_factory, tenant_id=tenant_id, item_id=item_id)
    except Exception as exc:
        logger.warning(
            "knowledge.embed_failed",
            item_id=str(item_id),
            tenant_id=str(tenant_id),
            error=str(exc)[:300],
        )


def _current(claimed: _Claimed) -> list:
    return [
        KnowledgeItem.id == claimed.id,
        KnowledgeItem.tenant_id == claimed.tenant_id,
        KnowledgeItem.status == KnowledgeItemStatus.PROCESSING,
        KnowledgeItem.updated_at == claimed.updated_at,
    ]


async def _finish(
    session_factory: SessionFactory,
    claimed: _Claimed,
    contents: list[str],
    truncated: bool,
    extra: dict | None = None,
) -> bool:
    """Replace the passages and set `ready`; `False` when the item changed meanwhile."""
    heading = claimed.title
    async with session_factory() as session:
        result = await session.execute(
            update(KnowledgeItem)
            .where(*_current(claimed))
            .values(status=KnowledgeItemStatus.READY, error=None, claimed_until=None,
                    truncated=truncated, **(extra or {}))
        )
        if result.rowcount != 1:  # type: ignore[attr-defined]
            await session.rollback()
            return False
        await session.execute(
            delete(KnowledgePassage).where(
                KnowledgePassage.tenant_id == claimed.tenant_id,
                KnowledgePassage.item_id == claimed.id,
            )
        )
        if contents:
            await session.execute(
                insert(KnowledgePassage),
                [
                    {
                        "id": uuid.uuid4(),
                        "tenant_id": claimed.tenant_id,
                        "item_id": claimed.id,
                        "position": position,
                        "heading": heading,
                        "content": content,
                        "search_text": normalize(f"{heading}\n{content}"),
                    }
                    for position, content in enumerate(contents)
                ],
            )
        await session.commit()
    return True


async def _fail(session_factory: SessionFactory, claimed: _Claimed, error: str) -> bool:
    async with session_factory() as session:
        result = await session.execute(
            update(KnowledgeItem)
            .where(*_current(claimed))
            .values(
                status=KnowledgeItemStatus.FAILED,
                error=error[:_MAX_ERROR_CHARS],
                claimed_until=None,
                source_key=None,
            )
        )
        await session.commit()
    return result.rowcount == 1  # type: ignore[attr-defined]


async def _drop_file(claimed: _Claimed) -> None:
    """The stored file is deleted once processed; Parley keeps only the text."""
    if claimed.item.source_key:
        try:
            await get_media_storage().delete(claimed.item.source_key)
        except Exception:
            logger.exception("knowledge.file_delete_failed", item_id=str(claimed.id))


async def process_due(session_factory: SessionFactory) -> int:
    """One pass: split every item in `processing` that is free. Returns how many
    items became ready or failed."""
    processed = 0
    for _ in range(MAX_ITEMS_PER_PASS):
        claimed = await _claim(session_factory, datetime.now(UTC))
        if claimed is None:
            break
        extra: dict = {}
        try:
            text, truncated = await extract_text(claimed.item)
            if claimed.item.kind == KnowledgeItemKind.DOCUMENT:
                if claimed.item.source_key:
                    await _check_total(session_factory, claimed, len(text))
                extra = {"body": text, "chars": len(text), "source_key": None}
            contents = split_passages(claimed.title, text)
        except Exception as exc:
            logger.exception(
                "knowledge.ingest_failed",
                item_id=str(claimed.id),
                tenant_id=str(claimed.tenant_id),
            )
            if await _fail(session_factory, claimed, str(exc) or type(exc).__name__):
                await _drop_file(claimed)
            processed += 1
            continue
        if not await _finish(session_factory, claimed, contents, truncated, extra):
            logger.info(
                "knowledge.ingest_superseded",
                item_id=str(claimed.id),
                tenant_id=str(claimed.tenant_id),
            )
            continue
        await _drop_file(claimed)
        processed += 1
        try:
            await after_passages(session_factory, claimed.tenant_id, claimed.id)
        except Exception:
            logger.exception(
                "knowledge.after_passages_failed",
                item_id=str(claimed.id),
                tenant_id=str(claimed.tenant_id),
            )
    return processed
