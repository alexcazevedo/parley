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
        """Non-expired memories, most important first, then newest, then id."""
        now = datetime.now(UTC)
        stmt = (
            select(Memory)
            .where(
                Memory.tenant_id == tenant_id,
                Memory.contact_id == contact_id,
                or_(Memory.expires_at.is_(None), Memory.expires_at > now),
            )
            .order_by(Memory.importance.desc(), Memory.created_at.desc(), Memory.id)
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def find_active_by_normalized_content(
        self, tenant_id: uuid.UUID, contact_id: uuid.UUID, normalized: str
    ) -> Memory | None:
        """The active memory whose trimmed, whitespace-collapsed, lower-cased
        content equals `normalized` (already normalized by the caller)."""
        now = datetime.now(UTC)
        stmt = (
            select(Memory)
            .where(
                Memory.tenant_id == tenant_id,
                Memory.contact_id == contact_id,
                or_(Memory.expires_at.is_(None), Memory.expires_at > now),
                func.lower(func.regexp_replace(func.btrim(Memory.content), r"\s+", " ", "g"))
                == normalized,
            )
            .order_by(Memory.created_at, Memory.id)
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalars().first()

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
