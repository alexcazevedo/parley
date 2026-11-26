"""Long-term memory: durable facts, preferences and issues the agent records
about a contact.

Retrieval is structured SQL today (`search` is a plain ILIKE); the type
signatures here are the seam a vector-similarity implementation would slot
into later without touching callers (`app/tools/builtin.py`'s `remember`
tool, `app/agents/runtime.py`'s context load, `app/api/contacts.py`'s REST
CRUD).
"""

import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.memory import Memory, MemoryType
from app.repositories.memories import MemoriesRepository

DEFAULT_LIMIT = 20


class MemoryService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = MemoriesRepository(session)

    async def store(
        self,
        tenant_id: uuid.UUID,
        contact_id: uuid.UUID,
        type: MemoryType | str,
        content: str,
        importance: int = 3,
        expires_at: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Memory:
        memory_type = type if isinstance(type, MemoryType) else MemoryType(type)
        normalized = re.sub(r"\s+", " ", content.strip()).lower()
        existing = await self._repo.find_active_by_normalized_content(
            tenant_id, contact_id, normalized
        )
        if existing is not None:
            if importance > existing.importance:
                existing.importance = importance
                await self._session.flush()
            return existing
        return await self._repo.create(
            tenant_id,
            contact_id=contact_id,
            type=memory_type,
            content=content,
            importance=importance,
            expires_at=expires_at,
            # `Memory.meta` maps onto the DB column "metadata"; see the model.
            meta=metadata or {},
        )

    async def get_contact_memories(
        self, tenant_id: uuid.UUID, contact_id: uuid.UUID, limit: int = DEFAULT_LIMIT
    ) -> list[Memory]:
        """Non-expired memories for `contact_id`: importance first, then newest.

        Read-only: nothing is written or flushed. The `last_accessed_at`
        column stays but is no longer set by anything.
        """
        return await self._repo.list_active_for_contact(tenant_id, contact_id, limit=limit)

    async def search(
        self, tenant_id: uuid.UUID, contact_id: uuid.UUID, query: str, limit: int = DEFAULT_LIMIT
    ) -> list[Memory]:
        """Substring match on `content`. Docstring, not code, is the seam for
        swapping this to vector similarity later — see the module docstring.
        """
        if not query.strip():
            return []
        return await self._repo.search_content(tenant_id, contact_id, query, limit=limit)

    async def forget(self, tenant_id: uuid.UUID, memory_id: uuid.UUID) -> bool:
        """Delete a memory. Tenant-scoped: cross-tenant ids silently no-op."""
        return await self._repo.delete(tenant_id, memory_id)
