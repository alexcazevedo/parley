"""Read-only repository for conversations — mutation endpoints land in a later ticket."""

import uuid

from sqlalchemy import select

from app.models.conversation import Conversation, ConversationState
from app.repositories.base import BaseRepository


class ConversationsRepository(BaseRepository[Conversation]):
    model = Conversation

    async def list_for_inbox(
        self,
        tenant_id: uuid.UUID,
        states: list[ConversationState] | None,
        limit: int,
    ) -> list[Conversation]:
        """Newest-activity-first page for the operator inbox/Ativas lists —
        unlike `BaseRepository.list` (oldest-created-first, single equality
        filters), this orders by `last_message_at DESC NULLS LAST,
        created_at DESC` and accepts a multi-value state filter.
        """
        stmt = select(Conversation).where(Conversation.tenant_id == tenant_id)
        if states:
            stmt = stmt.where(Conversation.state.in_(states))
        stmt = stmt.order_by(
            Conversation.last_message_at.desc().nulls_last(),
            Conversation.created_at.desc(),
        ).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())
