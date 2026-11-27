"""Long-term memory repository: the tenant + contact scoped queries
`app.memory.service.MemoryService` composes into store/search/forget.
"""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, or_, select

from app.models.memory import Memory, MemoryType
from app.repositories.base import BaseRepository


def recap_ended_at(memory: Memory) -> datetime:
    """When the recap's session ended (`metadata.ended_at`), else when it was stored."""
    raw = (memory.meta or {}).get("ended_at")
    if isinstance(raw, str):
        try:
            at = datetime.fromisoformat(raw)
        except ValueError:
            at = None
        if at is not None:
            return at if at.tzinfo else at.replace(tzinfo=UTC)
    return memory.created_at


class MemoriesRepository(BaseRepository[Memory]):
    model = Memory

    async def list_active_for_contact(
        self,
        tenant_id: uuid.UUID,
        contact_id: uuid.UUID,
        *,
        limit: int | None = None,
        include_recaps: bool = False,
    ) -> list[Memory]:
        """Non-expired facts (session recaps left out unless asked), most
        important first, then newest, then id."""
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
        if not include_recaps:
            stmt = stmt.where(Memory.type != MemoryType.SESSION_RECAP)
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_recaps(
        self,
        tenant_id: uuid.UUID,
        contact_id: uuid.UUID,
        *,
        limit: int,
        max_age_days: int,
    ) -> list[Memory]:
        """The most recent session recaps by session end, newest first.

        Session end is `metadata.ended_at`, falling back to `created_at`.
        Done in Python: a contact has few recaps and the ISO strings sit in JSON.
        """
        if limit <= 0:
            return []
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=max_age_days)
        stmt = select(Memory).where(
            Memory.tenant_id == tenant_id,
            Memory.contact_id == contact_id,
            Memory.type == MemoryType.SESSION_RECAP,
            or_(Memory.expires_at.is_(None), Memory.expires_at > now),
        )
        rows = list((await self.session.execute(stmt)).scalars().all())
        dated = [(recap_ended_at(m), m) for m in rows]
        dated = [(at, m) for at, m in dated if at >= cutoff]
        dated.sort(key=lambda pair: (pair[0], str(pair[1].id)), reverse=True)
        return [m for _, m in dated[:limit]]

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
