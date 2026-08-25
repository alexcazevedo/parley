"""Debounce scheduler: drains due conversations into turns.

Runs as an asyncio task inside the worker process alongside the job loop.
`tick` is the whole unit of work — tests drive it directly with an explicit
`now`, `main` just calls it on a 250ms cadence.

Claiming a conversation is two-stage: `ZREM` from `harness:due` (only one
worker gets the 1) *and* the per-conversation lock (protects against a worker
that is still mid-turn from an earlier claim). Losing the lock re-arms the
conversation a second out instead of dropping it.
"""

import asyncio
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.conversations import debounce, lock
from app.conversations.turn import TurnRuntime, process_turn
from app.models.message import Message

logger = structlog.get_logger(__name__)

TICK_INTERVAL_SECONDS = 0.25
DUE_BATCH_SIZE = 50
# How far out to push a conversation whose lock is held by another worker.
LOCK_RETRY_DELAY_SECONDS = 1.0

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


async def _load_messages(session: AsyncSession, message_ids: list[uuid.UUID]) -> list[Message]:
    result = await session.execute(
        select(Message).where(Message.id.in_(message_ids)).order_by(Message.created_at)
    )
    by_id = {message.id: message for message in result.scalars().all()}
    # Start from arrival (pending-list) order, then sort by created_at. The
    # sort is stable, so messages that share a timestamp keep arrival order.
    ordered = [by_id[mid] for mid in message_ids if mid in by_id]
    ordered.sort(key=lambda message: message.created_at)
    return ordered


async def tick(
    redis: Redis,
    session_factory: SessionFactory,
    runtime: TurnRuntime | None = None,
    *,
    now: float | None = None,
    limit: int = DUE_BATCH_SIZE,
) -> list[uuid.UUID]:
    """Process every conversation whose debounce window closed by `now`.

    Returns the conversation ids this call actually took a turn for.
    """
    now = time.time() if now is None else now
    processed: list[uuid.UUID] = []

    for raw_conversation_id in await debounce.due_conversations(redis, now=now, limit=limit):
        if not await debounce.claim(redis, raw_conversation_id):
            continue  # another worker took it

        token = await lock.acquire(redis, raw_conversation_id)
        if token is None:
            await debounce.schedule(
                redis, raw_conversation_id, time.time() + LOCK_RETRY_DELAY_SECONDS
            )
            logger.info("scheduler.lock_busy", conversation_id=raw_conversation_id)
            continue

        conversation_id = uuid.UUID(raw_conversation_id)
        try:
            message_ids = await debounce.drain_pending(redis, raw_conversation_id)
            if not message_ids:
                continue
            async with session_factory() as session:
                messages = await _load_messages(session, message_ids)
                await process_turn(session, conversation_id, messages, runtime)
            processed.append(conversation_id)
        finally:
            await lock.release(redis, raw_conversation_id, token)

        # Messages that arrived while we held the lock need their own turn.
        if await debounce.has_pending(redis, raw_conversation_id):
            await debounce.schedule(redis, raw_conversation_id, time.time())

    return processed


async def run_scheduler(
    redis: Redis,
    session_factory: SessionFactory,
    runtime: TurnRuntime | None = None,
    interval: float = TICK_INTERVAL_SECONDS,
) -> None:
    logger.info("scheduler.starting", interval=interval)
    while True:
        try:
            await tick(redis, session_factory, runtime)
        except Exception:  # a bad turn must not kill the scheduler loop
            logger.exception("scheduler.tick_failed")
        await asyncio.sleep(interval)
