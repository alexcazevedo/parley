"""Repository for conversations: inbox/line reads and the claim/release transitions."""

import uuid

from sqlalchemy import func, select, update

from app.models.conversation import Conversation, ConversationState
from app.repositories.base import BaseRepository


class ConversationsRepository(BaseRepository[Conversation]):
    model = Conversation

    async def list_for_inbox(
        self,
        tenant_id: uuid.UUID,
        states: list[ConversationState] | None,
        limit: int,
        *,
        category_id: uuid.UUID | None = None,
        uncategorized: bool = False,
        claimed_by: str | None = None,
    ) -> list[Conversation]:
        """Newest-activity-first page for the operator inbox/Ativas lists —
        unlike `BaseRepository.list` (oldest-created-first, single equality
        filters), this orders by `last_message_at DESC NULLS LAST,
        created_at DESC` and accepts a multi-value state filter.
        """
        stmt = select(Conversation).where(Conversation.tenant_id == tenant_id)
        if states:
            stmt = stmt.where(Conversation.state.in_(states))
        if category_id is not None:
            stmt = stmt.where(Conversation.category_id == category_id)
        if uncategorized:
            stmt = stmt.where(Conversation.category_id.is_(None))
        if claimed_by is not None:
            stmt = stmt.where(Conversation.claimed_by == claimed_by)
        stmt = stmt.order_by(
            Conversation.last_message_at.desc().nulls_last(),
            Conversation.created_at.desc(),
        ).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_waiting(self, tenant_id: uuid.UUID, limit: int) -> list[Conversation]:
        """Unclaimed `HUMAN_HANDOFF` conversations, oldest handoff first
        (`waiting_since` ascending, nulls last, then `id`).
        """
        result = await self.session.execute(
            select(Conversation)
            .where(
                Conversation.tenant_id == tenant_id,
                Conversation.state == ConversationState.HUMAN_HANDOFF,
                Conversation.claimed_by.is_(None),
            )
            .order_by(Conversation.waiting_since.asc().nulls_last(), Conversation.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def claim(
        self, tenant_id: uuid.UUID, conversation_id: uuid.UUID, operator_id: str
    ) -> Conversation | None:
        """One conditional UPDATE: an operator takes a waiting conversation that
        nobody else holds (or re-claims their own, keeping the original
        `claimed_at`). `None` when no row matched. Does not commit.
        """
        result = await self.session.execute(
            update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == tenant_id,
                Conversation.state == ConversationState.HUMAN_HANDOFF,
                (Conversation.claimed_by.is_(None)) | (Conversation.claimed_by == operator_id),
            )
            .values(
                claimed_by=operator_id,
                claimed_at=func.coalesce(Conversation.claimed_at, func.now()),
            )
            .returning(Conversation)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()

    async def release(
        self, tenant_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> Conversation | None:
        """One conditional UPDATE: clears the claim; `waiting_since` is kept so
        the conversation returns to its place in line. `None` when not claimed.
        """
        result = await self.session.execute(
            update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.tenant_id == tenant_id,
                Conversation.claimed_by.is_not(None),
            )
            .values(claimed_by=None, claimed_at=None)
            .returning(Conversation)
            .execution_options(populate_existing=True)
        )
        return result.scalar_one_or_none()
