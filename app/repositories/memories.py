"""Long-term memory repository: the tenant + contact scoped queries
`app.memory.service.MemoryService` composes into store/search/forget.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, or_, select

from app.models.memory import Memory
from app.repositories.base import BaseRepository


class MemoriesRepository(BaseRepository[Memory]):
    model = Memory

    async def list_active_for_contact(
        self, tenant_id: uuid.UUID, contact_id: uuid.UUID, *, limit: int | None = None
    ) -> list[Memory]:
        """Non-expired memories, most important and most recently touched first.

        `Memory` has no `updated_at` column, so `last_accessed_at` (falling
        back to `created_at` for a memory that was never re-read) is the
        recency tiebreaker.
        """
        now = datetime.now(UTC)
        stmt = (
            select(Memory)
            .where(
                Memory.tenant_id == tenant_id,
                Memory.contact_id == contact_id,
                or_(Memory.expires_at.is_(None), Memory.expires_at > now),
            )
            .order_by(
                Memory.importance.desc(),
                func.coalesce(Memory.last_accessed_at, Memory.created_at).desc(),
            )
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def search_content(
        self,
        tenant_id: uuid.UUID,
        contact_id: uuid.UUID,
        query: str,
        *,
        limit: int | None = 20,
    ) -> list[Memory]:
        """ILIKE substring search over `content`.

        Swap for a vector similarity query later (PRODUCT.md: "structured SQL
        is; interface allows semantic retrieval later") — callers only see
        `MemoryService.search`, so this is the one place that would change.
        """
        now = datetime.now(UTC)
        stmt = (
            select(Memory)
            .where(
                Memory.tenant_id == tenant_id,
                Memory.contact_id == contact_id,
                Memory.content.ilike(f"%{query}%"),
                or_(Memory.expires_at.is_(None), Memory.expires_at > now),
            )
            .order_by(Memory.importance.desc(), Memory.created_at.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
