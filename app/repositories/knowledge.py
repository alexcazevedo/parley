import uuid
from collections.abc import Iterable

from sqlalchemy import func, select

from app.models.knowledge_item import KnowledgeItem, KnowledgeItemKind, KnowledgeItemStatus
from app.models.knowledge_passage import KnowledgePassage
from app.repositories.base import BaseRepository


class KnowledgeItemsRepository(BaseRepository[KnowledgeItem]):
    model = KnowledgeItem

    async def get(self, tenant_id: uuid.UUID, id: uuid.UUID) -> KnowledgeItem | None:
        # populate_existing: the worker changes `status` in its own session,
        # so a row already in this session's identity map is read again.
        result = await self.session.execute(
            select(KnowledgeItem)
            .where(KnowledgeItem.tenant_id == tenant_id, KnowledgeItem.id == id)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def count(self, tenant_id: uuid.UUID) -> int:
        total = await self.session.scalar(
            select(func.count()).select_from(KnowledgeItem).where(
                KnowledgeItem.tenant_id == tenant_id
            )
        )
        return int(total or 0)

    async def total_chars(self, tenant_id: uuid.UUID) -> int:
        total = await self.session.scalar(
            select(func.coalesce(func.sum(KnowledgeItem.chars), 0)).where(
                KnowledgeItem.tenant_id == tenant_id
            )
        )
        return int(total or 0)

    async def get_by_title(
        self, tenant_id: uuid.UUID, kind: KnowledgeItemKind, title: str
    ) -> KnowledgeItem | None:
        result = await self.session.execute(
            select(KnowledgeItem)
            .where(
                KnowledgeItem.tenant_id == tenant_id,
                KnowledgeItem.kind == kind,
                KnowledgeItem.title == title,
            )
            .order_by(KnowledgeItem.created_at, KnowledgeItem.id)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def titles(self, tenant_id: uuid.UUID, limit: int) -> tuple[list[str], int]:
        """Titles of the ready items, oldest first, at most `limit`; and how many there are."""
        ready = (
            KnowledgeItem.tenant_id == tenant_id,
            KnowledgeItem.status == KnowledgeItemStatus.READY,
        )
        rows = await self.session.scalars(
            select(KnowledgeItem.title)
            .where(*ready)
            .order_by(KnowledgeItem.created_at, KnowledgeItem.id)
            .limit(limit)
        )
        total = await self.session.scalar(
            select(func.count()).select_from(KnowledgeItem).where(*ready)
        )
        return list(rows.all()), int(total or 0)

    async def passage_counts(
        self, tenant_id: uuid.UUID, item_ids: Iterable[uuid.UUID]
    ) -> dict[uuid.UUID, int]:
        ids = list(item_ids)
        if not ids:
            return {}
        rows = await self.session.execute(
            select(KnowledgePassage.item_id, func.count())
            .where(KnowledgePassage.tenant_id == tenant_id, KnowledgePassage.item_id.in_(ids))
            .group_by(KnowledgePassage.item_id)
        )
        return {item_id: int(n) for item_id, n in rows.tuples().all()}
