"""`HandoffService`: the only place conversation state moves in and out of
`HUMAN_HANDOFF`.

Three transitions, each a plain state check plus a `Handoff` row:

  * `handoff`  — `ACTIVE_AI`/`WAITING` -> `HUMAN_HANDOFF`, inserts a `Handoff`.
  * `resume`   — `HUMAN_HANDOFF` -> `ACTIVE_AI`, closes the open `Handoff`
                 (sets `resolved_at`).
  * `close`    — any state -> `CLOSED`.

Anything else raises `IllegalTransitionError`; callers (the built-in tool in
`app/tools/builtin.py`, the REST endpoints in `app/api/conversations.py`) map
that to a tool failure / `409` respectively.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation, ConversationState
from app.models.handoff import Handoff, HandoffRequestedBy

_HANDOFF_SOURCE_STATES = (ConversationState.ACTIVE_AI, ConversationState.WAITING)


class IllegalTransitionError(Exception):
    """Raised when a conversation is not in a state that allows the requested
    transition (e.g. resuming a conversation that isn't in `HUMAN_HANDOFF`).
    """


class HandoffService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def handoff(
        self,
        conversation: Conversation,
        reason: str | None,
        requested_by: HandoffRequestedBy,
    ) -> Handoff:
        if conversation.state not in _HANDOFF_SOURCE_STATES:
            raise IllegalTransitionError(
                f"cannot hand off a conversation in state {conversation.state}"
            )
        conversation.state = ConversationState.HUMAN_HANDOFF
        record = Handoff(
            id=uuid.uuid4(),
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            reason=reason,
            requested_by=requested_by,
        )
        self._session.add(record)
        await self._session.flush()
        return record

    async def resume(self, conversation: Conversation) -> Conversation:
        if conversation.state != ConversationState.HUMAN_HANDOFF:
            raise IllegalTransitionError(
                f"cannot resume a conversation in state {conversation.state}"
            )
        conversation.state = ConversationState.ACTIVE_AI
        open_handoff = await self._latest_open_handoff(conversation)
        if open_handoff is not None:
            open_handoff.resolved_at = datetime.now(UTC)
        await self._session.flush()
        return conversation

    async def close(self, conversation: Conversation) -> Conversation:
        conversation.state = ConversationState.CLOSED
        open_handoff = await self._latest_open_handoff(conversation)
        if open_handoff is not None:
            open_handoff.resolved_at = datetime.now(UTC)
        await self._session.flush()
        return conversation

    async def _latest_open_handoff(self, conversation: Conversation) -> Handoff | None:
        result = await self._session.execute(
            select(Handoff)
            .where(
                Handoff.tenant_id == conversation.tenant_id,
                Handoff.conversation_id == conversation.id,
                Handoff.resolved_at.is_(None),
            )
            .order_by(Handoff.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()
