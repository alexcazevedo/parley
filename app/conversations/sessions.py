"""Sessions: a conversation split into runs of messages with no long silence.

A session is a run of messages of one conversation with no silence longer
than the agent's `session_gap_hours`, counted from the last message of either
side (customer, agent or operator). Tool rows do not count.

`begin_turn` runs at the start of a turn, before the agent: when the time
between the newest message before the batch and the batch's first message is
more than the gap, the open session is ended (`pending`, `ended_by`
`silence`, the running summary copied to `summary_snapshot`) and a new one
opens at the batch's first message. The conversation's `summary` and
`summary_last_seq` are then cleared, so the running summary restarts with the
new session. The check runs under the conversation's row lock
(`SELECT ... FOR UPDATE`, the lock every message insert takes too) and the end
is one conditional `UPDATE ... WHERE status = 'open'`, so two workers cannot
both do it.

A conversation with no row yet (every conversation older than migration 0031)
is one open session covering all its messages; its row is created on first
need. No stored message is deleted or changed.

With `agent.session_memory_enabled` false nothing here runs: no row is
written, and readers (`current_session`) find none, so the history and the
running summary span the whole conversation as before.
"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import end_transaction
from app.models.agent import Agent
from app.models.conversation import Conversation
from app.models.conversation_session import ConversationSession, SessionEndedBy, SessionStatus
from app.models.message import Message, MessageRole

logger = structlog.get_logger(__name__)

# The messages a silence is counted from: either side, never tool plumbing.
SPOKEN_ROLES = (MessageRole.USER, MessageRole.ASSISTANT, MessageRole.HUMAN_OPERATOR)


def session_first_seq(row: ConversationSession | None) -> int | None:
    """The first `seq` of `row`, or None (no row: the whole conversation)."""
    return row.first_seq if row is not None else None


async def current_session(
    session: AsyncSession, conversation: Conversation
) -> ConversationSession | None:
    """The conversation's open session row, or None when it has none yet."""
    result = await session.execute(
        select(ConversationSession).where(
            ConversationSession.tenant_id == conversation.tenant_id,
            ConversationSession.conversation_id == conversation.id,
            ConversationSession.status == SessionStatus.OPEN.value,
        )
    )
    return result.scalars().first()


async def begin_turn(
    session: AsyncSession,
    agent: Agent,
    conversation: Conversation,
    batch: Sequence[Message],
) -> ConversationSession | None:
    """The session this turn belongs to, ending the open one after a long silence.

    Returns None when the behaviour is off (nothing is written) or the batch
    is empty. Commits what it wrote (`end_transaction`), which also releases
    the conversation lock before the turn's slow work.
    """
    if not agent.session_memory_enabled or not batch:
        return None
    gap = timedelta(hours=agent.session_gap_hours or 48)
    tenant_id = conversation.tenant_id

    summary = await _lock(session, conversation)
    first = (
        await session.execute(
            select(Message.seq, Message.created_at)
            .where(
                Message.tenant_id == tenant_id,
                Message.conversation_id == conversation.id,
                Message.id.in_([message.id for message in batch]),
            )
            .order_by(Message.seq)
            .limit(1)
        )
    ).first()
    if first is None:
        # The batch is not visible here: nothing to split on.
        row = await _ensure_open(session, conversation)
        await end_transaction(session)
        return row
    first_seq, first_at = first.seq, first.created_at

    row = await current_session(session, conversation)
    if row is not None and row.first_seq >= first_seq:
        # Already split at this batch (a retry) or the batch opened the session.
        await end_transaction(session)
        return row

    previous = (
        await session.execute(
            select(Message.seq, Message.created_at)
            .where(
                Message.tenant_id == tenant_id,
                Message.conversation_id == conversation.id,
                Message.seq < first_seq,
                Message.role.in_(SPOKEN_ROLES),
            )
            .order_by(Message.seq.desc())
            .limit(1)
        )
    ).first()
    if row is None:
        row = await _ensure_open(session, conversation)
    if row is None or previous is None or first_at - previous.created_at <= gap:
        await end_transaction(session)
        return row
    if previous.seq < row.first_seq:
        # The message before this batch belongs to a session that already
        # ended (the worker's silence pass, or a close): the open row starts
        # after it, so there is nothing left to end here.
        await end_transaction(session)
        return row

    ended = await _end(
        session,
        conversation,
        row,
        last_seq=previous.seq,
        ended_at=previous.created_at,
        ended_by=SessionEndedBy.SILENCE,
        summary=summary,
    )
    if not ended:
        # Someone else ended it first; the open row is theirs.
        current = await current_session(session, conversation)
        await end_transaction(session)
        return current
    new_row = ConversationSession(
        tenant_id=tenant_id,
        conversation_id=conversation.id,
        contact_id=conversation.contact_id,
        first_seq=first_seq,
        started_at=first_at,
        status=SessionStatus.OPEN.value,
    )
    session.add(new_row)
    await session.flush()
    logger.info(
        "session.started",
        conversation_id=str(conversation.id),
        ended_session_id=str(row.id),
        first_seq=first_seq,
    )
    await end_transaction(session)
    return new_row


async def end_open_session(
    session: AsyncSession, conversation: Conversation, *, ended_by: str
) -> ConversationSession | None:
    """End the conversation's open session at its newest message; flushed, not committed.

    A conversation with no row yet gets one first. Returns the ended row, or
    None when there is nothing to end (no message at all, or another worker
    ended it first).
    """
    summary = await _lock(session, conversation)
    row = await current_session(session, conversation)
    if row is None:
        row = await _ensure_open(session, conversation)
        if row is None:
            return None
    last = (
        await session.execute(
            select(Message.seq, Message.created_at)
            .where(
                Message.tenant_id == conversation.tenant_id,
                Message.conversation_id == conversation.id,
                Message.seq >= row.first_seq,
                Message.role.in_(SPOKEN_ROLES),
            )
            .order_by(Message.seq.desc())
            .limit(1)
        )
    ).first()
    ended = await _end(
        session,
        conversation,
        row,
        last_seq=last.seq if last is not None else row.first_seq,
        ended_at=last.created_at if last is not None else datetime.now(UTC),
        ended_by=ended_by,
        summary=summary,
    )
    return row if ended else None


async def _lock(session: AsyncSession, conversation: Conversation) -> str | None:
    """Lock the conversation row; returns its running summary as of now."""
    return await session.scalar(
        select(Conversation.summary)
        .where(Conversation.id == conversation.id, Conversation.tenant_id == conversation.tenant_id)
        .with_for_update()
    )


async def _ensure_open(
    session: AsyncSession, conversation: Conversation
) -> ConversationSession | None:
    """The open row, created when missing; caller holds the conversation lock.

    A new row starts at the first message after the last ended session (the
    first message of the conversation when none ended). None when the
    conversation has no message to start one at.
    """
    row = await current_session(session, conversation)
    if row is not None:
        return row
    after = await session.scalar(
        select(func.max(ConversationSession.last_seq)).where(
            ConversationSession.tenant_id == conversation.tenant_id,
            ConversationSession.conversation_id == conversation.id,
        )
    )
    query = select(Message.seq, Message.created_at).where(
        Message.tenant_id == conversation.tenant_id,
        Message.conversation_id == conversation.id,
    )
    if after is not None:
        query = query.where(Message.seq > after)
    start = (await session.execute(query.order_by(Message.seq).limit(1))).first()
    if start is None:
        return None
    row = ConversationSession(
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        contact_id=conversation.contact_id,
        first_seq=start.seq,
        started_at=start.created_at,
        status=SessionStatus.OPEN.value,
    )
    session.add(row)
    await session.flush()
    return row


async def _end(
    session: AsyncSession,
    conversation: Conversation,
    row: ConversationSession,
    *,
    last_seq: int,
    ended_at: datetime,
    ended_by: str,
    summary: str | None,
) -> bool:
    """One conditional write: `row` goes from `open` to `pending`, or nothing happens.

    On success the conversation's running summary is cleared. Returns whether
    this call ended it.
    """
    result = await session.execute(
        update(ConversationSession)
        .where(
            ConversationSession.id == row.id,
            ConversationSession.tenant_id == conversation.tenant_id,
            ConversationSession.status == SessionStatus.OPEN.value,
        )
        .values(
            status=SessionStatus.PENDING.value,
            last_seq=last_seq,
            ended_at=ended_at,
            ended_by=str(ended_by),
            summary_snapshot=summary,
        )
        .execution_options(synchronize_session="fetch")
    )
    if getattr(result, "rowcount", 0) != 1:
        return False
    conversation.summary = None
    conversation.summary_last_seq = None
    await session.flush()
    logger.info(
        "session.ended",
        conversation_id=str(conversation.id),
        session_id=str(row.id),
        ended_by=str(ended_by),
        last_seq=last_seq,
    )
    return True
