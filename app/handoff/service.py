"""`HandoffService`: the only place conversation state moves in and out of
`HUMAN_HANDOFF`.

Three transitions, each a state check plus a `Handoff` row:

  * `handoff`  — `ACTIVE_AI`/`WAITING` -> `HUMAN_HANDOFF`, inserts a `Handoff`.
  * `resume`   — `HUMAN_HANDOFF` -> `ACTIVE_AI`, closes the open `Handoff`
                 (sets `resolved_at`).
  * `close`    — any state -> `CLOSED`; also ends the open session
                 (`ended_by` `closed`, app/conversations/sessions.py) when
                 the agent has `session_memory_enabled`; its recap is
                 written later by the worker (app/conversations/session_end.py).

Anything else raises `IllegalTransitionError`; callers (the built-in tool in
`app/tools/builtin.py`, the REST endpoints in `app/api/conversations.py`) map
that to a tool failure / `409` respectively.

Each transition decides on the conversation's current row, not on the loaded
`Conversation` (PRODUCT.md E4.2): it re-reads the state with `SELECT ... FOR
UPDATE`, so a stale object (a turn's, loaded before an operator closed the
conversation) cannot overwrite a newer state. The row lock lasts until the
caller commits; callers commit right after (the tool ledger, the endpoints,
the sentiment path's send), never across a model call. The loaded object is
not refreshed by the re-read: on a refusal it keeps its old state, which is
what lets a running turn notice the change (`app/conversations/turn.py`).

`waiting_since` (the place in the waiting line) and the claim columns are
written here too, by a core UPDATE: `handoff` stamps `waiting_since` and clears
the claim, `resume` and `close` clear all three. Nothing else writes
`waiting_since`.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.conversations.sessions import end_open_session
from app.events.outbox import (
    CAUSE_AGENT_HANDOFF,
    CAUSE_CLOSED,
    CAUSE_OPERATOR,
    CAUSE_RESUMED,
    CAUSE_SENTIMENT,
    CONVERSATION_AGENT,
    CONVERSATION_HANDOFF,
    record_event,
)
from app.models.agent import Agent
from app.models.conversation import Conversation, ConversationState
from app.models.conversation_session import SessionEndedBy
from app.models.handoff import Handoff, HandoffRequestedBy

# `conversation.agent` cause by who asked for the handoff, when the caller gives none.
_CAUSE_BY_REQUESTER = {
    HandoffRequestedBy.AGENT: CAUSE_AGENT_HANDOFF,
    HandoffRequestedBy.SYSTEM: CAUSE_SENTIMENT,
    HandoffRequestedBy.OPERATOR: CAUSE_OPERATOR,
    HandoffRequestedBy.USER: CAUSE_OPERATOR,
}

_HANDOFF_SOURCE_STATES = (ConversationState.ACTIVE_AI, ConversationState.WAITING)


class IllegalTransitionError(Exception):
    """Raised when a conversation is not in a state that allows the requested
    transition (e.g. resuming a conversation that isn't in `HUMAN_HANDOFF`).
    """


class HandoffService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def ensure_can_handoff(self, conversation: Conversation) -> None:
        """Early refusal on the loaded object, before any write or model call.

        Only a pre-check: `handoff` decides again on the current row.
        """
        _check_can_handoff(conversation.state)

    async def handoff(
        self,
        conversation: Conversation,
        reason: str | None,
        requested_by: HandoffRequestedBy,
        *,
        cause: str | None = None,
        brief: str | None = None,
    ) -> Handoff:
        """Hand off. On an `external` channel the same transaction records
        `conversation.handoff` and `conversation.agent` (`active` false); `cause`
        defaults from `requested_by`, `brief` is the summary when already written.
        """
        _check_can_handoff(await self._locked_state(conversation))
        self._set_state(conversation, ConversationState.HUMAN_HANDOFF)
        record = Handoff(
            id=uuid.uuid4(),
            tenant_id=conversation.tenant_id,
            conversation_id=conversation.id,
            reason=reason,
            requested_by=requested_by,
            summary=brief,
        )
        self._session.add(record)
        await self._session.flush()
        await self._write_line_columns(conversation, entering=True)
        await record_event(
            self._session,
            conversation,
            CONVERSATION_HANDOFF,
            {"reason": reason, "requested_by": str(requested_by), "brief": brief},
        )
        await record_event(
            self._session,
            conversation,
            CONVERSATION_AGENT,
            {"active": False, "cause": cause or _CAUSE_BY_REQUESTER[requested_by]},
        )
        return record

    async def resume(self, conversation: Conversation) -> Conversation:
        current = await self._locked_state(conversation)
        if current != ConversationState.HUMAN_HANDOFF:
            raise IllegalTransitionError(f"cannot resume a conversation in state {current}")
        self._set_state(conversation, ConversationState.ACTIVE_AI)
        open_handoff = await self._latest_open_handoff(conversation)
        if open_handoff is not None:
            open_handoff.resolved_at = datetime.now(UTC)
        await self._session.flush()
        await self._write_line_columns(conversation, entering=False)
        await record_event(
            self._session,
            conversation,
            CONVERSATION_AGENT,
            {"active": True, "cause": CAUSE_RESUMED},
        )
        return conversation

    async def close(self, conversation: Conversation) -> Conversation:
        await self._locked_state(conversation)
        self._set_state(conversation, ConversationState.CLOSED)
        open_handoff = await self._latest_open_handoff(conversation)
        if open_handoff is not None:
            open_handoff.resolved_at = datetime.now(UTC)
        await self._session.flush()
        await self._write_line_columns(conversation, entering=False)
        await self._end_session(conversation)
        await record_event(
            self._session,
            conversation,
            CONVERSATION_AGENT,
            {"active": False, "cause": CAUSE_CLOSED},
        )
        return conversation

    async def _end_session(self, conversation: Conversation) -> None:
        """End the open session (flushed, committed by the caller); off agents are skipped."""
        # The agent's switch, read here rather than through `load_conversation_agent`
        # (app.agents imports this module); same tenant check.
        enabled = await self._session.scalar(
            select(Agent.session_memory_enabled).where(
                Agent.id == conversation.agent_id, Agent.tenant_id == conversation.tenant_id
            )
        )
        if not enabled:
            return
        await end_open_session(self._session, conversation, ended_by=SessionEndedBy.CLOSED)

    async def _write_line_columns(self, conversation: Conversation, *, entering: bool) -> None:
        """`waiting_since` and the claim, in one core UPDATE on the row.

        Entering the line stamps the database clock and clears any claim;
        leaving it clears all three. Not assigned on the loaded object: the
        session is async and a SQL expression there would expire the attribute.
        The loaded object's three columns are stale until the caller refreshes.
        """
        values: dict[str, Any] = {"claimed_by": None, "claimed_at": None}
        values["waiting_since"] = func.now() if entering else None
        await self._session.execute(
            update(Conversation)
            .where(
                Conversation.tenant_id == conversation.tenant_id,
                Conversation.id == conversation.id,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )

    async def _locked_state(self, conversation: Conversation) -> ConversationState:
        """The row's current state, locked (`FOR UPDATE`) until the caller commits.

        A column select, so the loaded `conversation` is left as it was.
        """
        result = await self._session.execute(
            select(Conversation.state)
            .where(
                Conversation.tenant_id == conversation.tenant_id,
                Conversation.id == conversation.id,
            )
            .with_for_update()
        )
        return result.scalar_one()

    @staticmethod
    def _set_state(conversation: Conversation, state: ConversationState) -> None:
        # Always written, even when the loaded (possibly stale) value already
        # equals `state`: the row may hold something else by now.
        conversation.state = state
        flag_modified(conversation, "state")

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


def _check_can_handoff(state: ConversationState) -> None:
    if state not in _HANDOFF_SOURCE_STATES:
        raise IllegalTransitionError(f"cannot hand off a conversation in state {state}")
