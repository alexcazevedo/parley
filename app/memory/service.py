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
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversations.sessions import current_session
from app.models.conversation import Conversation
from app.models.conversation_session import ConversationSession, SessionStatus
from app.models.memory import Memory, MemoryType
from app.repositories.memories import MemoriesRepository, recap_ended_at

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
        return await self._repo.list_active_for_contact(
            tenant_id, contact_id, limit=limit, include_recaps=True
        )

    async def get_contact_facts(
        self, tenant_id: uuid.UUID, contact_id: uuid.UUID, limit: int = DEFAULT_LIMIT
    ) -> list[Memory]:
        """Like `get_contact_memories`, without the session recaps."""
        return await self._repo.list_active_for_contact(tenant_id, contact_id, limit=limit)

    async def get_recap_lines(
        self,
        conversation: Conversation,
        *,
        limit: int,
        max_age_days: int,
    ) -> list[str]:
        """The recaps a prompt shows, oldest first, as `DD/MM/YYYY: text` lines.

        The contact's most recent ones by session end, within `limit` and
        `max_age_days`, never the open session's own. A session whose recap is
        still `pending` is stood in for by its `summary_snapshot`, counted within
        the limit.
        """
        if limit <= 0:
            return []
        open_row = await current_session(self._session, conversation)
        recaps = await self._repo.list_recaps(
            conversation.tenant_id,
            conversation.contact_id,
            limit=limit + 1,
            max_age_days=max_age_days,
        )
        open_id = str(open_row.id) if open_row is not None else None
        entries: list[tuple[datetime, str, str]] = []
        seen: set[str] = set()
        for memory in recaps:
            session_id = str((memory.meta or {}).get("session_id") or "")
            if session_id:
                seen.add(session_id)
            if open_id is not None and session_id == open_id:
                continue
            entries.append((recap_ended_at(memory), str(memory.id), memory.content.strip()))
        cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
        pending = await self._session.execute(
            select(ConversationSession).where(
                ConversationSession.tenant_id == conversation.tenant_id,
                ConversationSession.contact_id == conversation.contact_id,
                ConversationSession.status == SessionStatus.PENDING.value,
                ConversationSession.ended_at.is_not(None),
                ConversationSession.ended_at >= cutoff,
            )
        )
        for row in pending.scalars().all():
            snapshot = (row.summary_snapshot or "").strip()
            if not snapshot or str(row.id) in seen or row.ended_at is None:
                continue
            line = f"{row.ended_at.astimezone(UTC).strftime('%d/%m/%Y')}: {snapshot}"
            entries.append((row.ended_at, str(row.id), line))
        entries.sort(key=lambda e: (e[0], e[1]), reverse=True)
        newest = entries[:limit]
        return [line for _, _, line in reversed(newest) if line]

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
