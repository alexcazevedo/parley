"""Debounce scheduler: drains due conversations into turns.

Runs as an asyncio task inside the worker process alongside the job loop.
`run_scheduler` is what the worker runs: it keeps a pool of turn tasks and,
every `TICK_INTERVAL_SECONDS` (250ms), fills the free slots with due
conversations (`_fill_slots`) without waiting for the turns already running.
`tick` is the run-to-completion variant: it claims what is due at an explicit
`now`, runs those turns side by side and returns when all are done. Most tests
drive `tick`; the worker does not call it.

Claiming a conversation is two-stage: `debounce.claim` (takes the claim key
`harness:claim:{id}` and moves its due score `CLAIM_TIMEOUT_SECONDS` out; only
one worker wins) *and* the per-conversation lock (protects against a worker
that is still mid-turn from an earlier claim). Finding the lock busy gives the
claim back and re-arms the conversation a second out
(`debounce.release_claim`) instead of dropping it.

Per conversation, `tick` does: claim -> lock -> drain (pending -> in-flight)
-> start heartbeat -> turn -> stop heartbeat -> confirm the lock -> `finish`
(or `return_inflight` + `finish` when superseded, or `_settle_failure` when
the turn failed) -> unlock.
The due entry is only removed by `finish`, under the lock, so a worker that
dies anywhere before it leaves the conversation due again after the claim
timeout (score and claim key both run out), with its batch still in the
in-flight list.

Lock heartbeat (PRODUCT.md E3.5): while the turn runs, a task renews the lock
and pushes the claim out every `lock.LOCK_RENEW_INTERVAL_SECONDS`, so a turn of
any length keeps both. The turn asks `_LockKeeper.still_owned` before each send
and commit. Ownership is lost for good when a renewal finds another token (or
none), or when no renewal succeeded for a whole TTL (Redis unreachable, event
loop stalled): past that point the key may have expired and been taken. A turn
that lost the lock touches no debounce state — whoever holds the lock now owns
the batch, and if nobody does, the claim timeout brings it back. One
exception: a turn whose reply was already sent and stored takes exactly those
message ids off the in-flight list (`debounce.drop_inflight`), so they are not
answered a second time.

Failed turns (PRODUCT.md E3.2): `process_turn` raises `TurnFailed` (any other
exception from the session or the batch load counts as one with nothing
delivered). Under a confirmed lock, `_settle_failure` then retries the batch
(`return_inflight` + `release_claim` at `RETRY_BACKOFF_SECONDS[n-1]` past the
moment of failure) up to `MAX_TURN_ATTEMPTS`, or dead-letters it
(`retry.TURN_DLQ_KEY`) with one
`send_failure_notice`. A reply that partly reached the customer is
dead-lettered at once, never retried. A failure never leaves the tick: the
next due conversation runs either way, and the lock is released.

Concurrency (PRODUCT.md E3.4): conversations run side by side, at most
`Settings.worker_concurrency` at once, each as its own task
(`TURN_TASK_PREFIX`) with its own session. The per-conversation sequence above
is `_take_turn`, unchanged; the claim happens just before it, and only once a
slot is free, so a claim never waits behind a slow turn. The same
conversation never runs twice at once: that is still the claim plus the lock.
An error in one conversation (even from the Redis bookkeeping) is logged as
`scheduler.turn_crashed` and never cancels the others. `run_scheduler` keeps
a pool across loops and, once `stop` is set, claims nothing new and waits for
the running turns.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.conversations import debounce, lock, retry
from app.conversations.turn import (
    AnyTurnRuntime,
    LockLost,
    Superseded,
    TurnFailed,
    process_turn,
    send_failure_notice,
)
from app.models.message import Message

logger = structlog.get_logger(__name__)

TICK_INTERVAL_SECONDS = 0.25
DUE_BATCH_SIZE = 50
# How far out to push a conversation whose lock is held by another worker.
LOCK_RETRY_DELAY_SECONDS = 1.0

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

HEARTBEAT_TASK_PREFIX = "lock-heartbeat:"
# Name prefix of the task running one conversation's turn.
TURN_TASK_PREFIX = "scheduler-turn:"


async def _pause(seconds: float) -> None:
    """Wait `seconds` without `asyncio.sleep`.

    Tests replace `asyncio.sleep` (module-wide) with a coroutine that returns
    at once; a heartbeat built on it would spin without ever yielding.
    """
    loop = asyncio.get_running_loop()
    waiter = loop.create_future()
    handle = loop.call_later(seconds, waiter.set_result, None)
    try:
        await waiter
    finally:
        handle.cancel()


class _LockKeeper:
    """Keeps one turn's lock (and claim) alive and says whether it is still ours.

    `valid_until` is a monotonic deadline: the latest moment the key is known
    to still carry our token. Each successful renewal moves it to the time the
    renewal was *sent* plus the TTL (Redis applied the TTL no earlier than
    that). Loss is sticky: an expired lock is never treated as ours again.
    """

    def __init__(
        self, redis: Redis, conversation_id: str, token: str, *, acquired_at: float
    ) -> None:
        self._redis = redis
        self._conversation_id = conversation_id
        self._token = token
        self._ttl_ms = lock.LOCK_TTL_MS
        self._interval = lock.LOCK_RENEW_INTERVAL_SECONDS
        self._valid_until = acquired_at + self._ttl_ms / 1000
        self._lost = False
        self._task: asyncio.Task | None = None

    @property
    def lost(self) -> bool:
        return self._lost

    async def still_owned(self) -> bool:
        """Renew once and report ownership. Never raises.

        A Redis error is not a loss by itself: the last renewal still covers
        us until `valid_until`.
        """
        if self._lost:
            return False
        sent_at = time.monotonic()
        if sent_at >= self._valid_until:
            self._lost = True
            return False
        try:
            owned = await lock.renew(
                self._redis, self._conversation_id, self._token, ttl_ms=self._ttl_ms
            )
        except Exception:
            logger.warning(
                "scheduler.lock_renew_failed",
                conversation_id=self._conversation_id,
                exc_info=True,
            )
            if time.monotonic() >= self._valid_until:
                self._lost = True
            return not self._lost
        if not owned:
            self._lost = True
        elif not self._lost:
            self._valid_until = max(self._valid_until, sent_at + self._ttl_ms / 1000)
        return not self._lost

    async def _beat(self) -> None:
        try:
            while True:
                await _pause(self._interval)
                if not await self.still_owned():
                    return  # nothing left to keep; the turn sees it at its next check
                try:
                    await debounce.extend_claim(self._redis, self._conversation_id)
                except Exception:
                    logger.warning(
                        "scheduler.claim_extend_failed",
                        conversation_id=self._conversation_id,
                        exc_info=True,
                    )
        except Exception:  # never let the heartbeat raise; the deadline covers us
            logger.exception("scheduler.heartbeat_failed", conversation_id=self._conversation_id)

    def start(self) -> None:
        self._task = asyncio.create_task(
            self._beat(), name=f"{HEARTBEAT_TASK_PREFIX}{self._conversation_id}"
        )

    async def stop(self) -> None:
        """Cancel the heartbeat and wait until it is gone. Never raises its errors."""
        if self._task is None:
            return
        self._task.cancel()
        # return_exceptions: the task's own CancelledError (or anything else)
        # stays here. If *this* task is cancelled meanwhile, that propagates.
        await asyncio.gather(self._task, return_exceptions=True)


async def _load_messages(session: AsyncSession, message_ids: list[uuid.UUID]) -> list[Message]:
    result = await session.execute(
        select(Message).where(Message.id.in_(message_ids)).order_by(Message.seq)
    )
    # Conversation order (`seq`), not arrival order and not any clock.
    return list(result.scalars().all())


def _delivered_before(exc: BaseException) -> int:
    """`delivered_parts` of a `TurnFailed` this exception was raised over, else 0."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        if isinstance(current, TurnFailed):
            return current.delivered_parts
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return 0


def _error_text(failure: BaseException) -> str:
    """What went wrong, for the dead-letter entry: the original error if chained."""
    original = failure.__cause__ if isinstance(failure, TurnFailed) else None
    error = original if original is not None else failure
    return f"{type(error).__name__}: {error}"[:2000]


async def _settle_failure(
    redis: Redis,
    session_factory: SessionFactory,
    raw_conversation_id: str,
    message_ids: list[uuid.UUID],
    failure: Exception,
    *,
    delivered_parts: int,
    now: float,
) -> None:
    """Retry, or dead-letter, the batch of a failed turn. Runs under the lock.

    - Something reached the customer: dead-letter it as partly delivered and
      finish. No retry (it would repeat what was sent), no error message.
    - Nothing did, below `MAX_TURN_ATTEMPTS`: put the batch back at the head
      of pending, give up the claim and make it due after the backoff,
      counted from now (the failure), not from the claim: a turn that took
      longer than the backoff must still wait it. A `now` later than the
      clock (tests drive `tick` with one) counts as the failure time.
      Messages that arrive in the meantime join it (`push_pending` cannot
      pull the retry earlier).
    - Nothing did, at the limit: dead-letter, finish, then the one error
      message. `finish` goes first so that a crash after it cannot bring the
      batch back for a second notice; the lock is still held, so the notice
      still lands before any later turn of this conversation.

    Never raises: one conversation's broken bookkeeping must not stop the rest
    of the tick. A step that fails leaves the batch in flight under its claim,
    and the claim timeout brings it back.
    """
    error = _error_text(failure)
    try:
        attempts = await retry.record_failure(redis, raw_conversation_id)
        if delivered_parts > 0:
            await retry.dead_letter(
                redis,
                raw_conversation_id,
                message_ids,
                error=error,
                partly_delivered=True,
                attempts=attempts,
            )
            await retry.clear(redis, raw_conversation_id)
            await debounce.finish(redis, raw_conversation_id)
            logger.error(
                "scheduler.turn_dead_lettered",
                conversation_id=raw_conversation_id,
                attempts=attempts,
                partly_delivered=True,
                delivered_parts=delivered_parts,
                message_count=len(message_ids),
                error=error,
                notice_stored=False,
            )
            return

        if attempts < retry.MAX_TURN_ATTEMPTS:
            backoff = retry.RETRY_BACKOFF_SECONDS[
                min(attempts, len(retry.RETRY_BACKOFF_SECONDS)) - 1
            ]
            failed_at = max(now, time.time())
            await debounce.return_inflight(redis, raw_conversation_id)
            await debounce.release_claim(redis, raw_conversation_id, failed_at + backoff)
            logger.warning(
                "scheduler.turn_retry",
                conversation_id=raw_conversation_id,
                attempt=attempts,
                retry_in_seconds=backoff,
                error=error,
            )
            return

        await retry.dead_letter(
            redis,
            raw_conversation_id,
            message_ids,
            error=error,
            partly_delivered=False,
            attempts=attempts,
        )
        await retry.clear(redis, raw_conversation_id)
        await debounce.finish(redis, raw_conversation_id)
    except Exception:
        logger.exception(
            "scheduler.turn_failure_unsettled", conversation_id=raw_conversation_id
        )
        return

    notice_stored = False
    try:
        async with session_factory() as session:
            notice_stored = await send_failure_notice(session, uuid.UUID(raw_conversation_id))
    except Exception:
        logger.exception("scheduler.failure_notice_failed", conversation_id=raw_conversation_id)
    logger.error(
        "scheduler.turn_dead_lettered",
        conversation_id=raw_conversation_id,
        attempts=attempts,
        partly_delivered=False,
        delivered_parts=0,
        message_count=len(message_ids),
        error=error,
        notice_stored=notice_stored,
    )


async def _take_turn(
    redis: Redis,
    session_factory: SessionFactory,
    runtime: AnyTurnRuntime | None,
    raw_conversation_id: str,
    *,
    now: float,
) -> uuid.UUID | None:
    """One claimed conversation: lock -> drain -> turn -> settle -> unlock.

    Returns the conversation id when a turn answered (or refused) its batch,
    `None` otherwise. Errors from the Redis bookkeeping itself (lock, drain,
    finish, release) escape; the callers below log them per conversation.
    """
    acquired_at = time.monotonic()
    token = await lock.acquire(redis, raw_conversation_id, ttl_ms=lock.LOCK_TTL_MS)
    if token is None:
        await debounce.release_claim(
            redis, raw_conversation_id, time.time() + LOCK_RETRY_DELAY_SECONDS
        )
        logger.info("scheduler.lock_busy", conversation_id=raw_conversation_id)
        return None

    conversation_id = uuid.UUID(raw_conversation_id)
    outcome: uuid.UUID | None = None
    try:
        # Pending -> in-flight; includes any batch a dead worker left behind.
        message_ids = await debounce.drain_pending(redis, raw_conversation_id)
        if not message_ids:
            await debounce.finish(redis, raw_conversation_id)
            return None

        async def check(raw_conversation_id: str = raw_conversation_id) -> bool:
            return await debounce.has_pending(redis, raw_conversation_id)

        keeper = _LockKeeper(redis, raw_conversation_id, token, acquired_at=acquired_at)
        keeper.start()
        result: object = None
        failure: Exception | None = None
        delivered_parts = 0
        turn_returned = False
        try:
            async with session_factory() as session:
                messages = await _load_messages(session, message_ids)
                result = await process_turn(
                    session,
                    conversation_id,
                    messages,
                    runtime,
                    supersede_check=check,
                    ownership_check=keeper.still_owned,
                )
                turn_returned = True
        except TurnFailed as exc:
            failure = exc
            delivered_parts = exc.delivered_parts
        except Exception as exc:
            if turn_returned:
                # Closing the session failed after the turn finished: its
                # outcome stands (a retry could only repeat a sent reply).
                logger.warning(
                    "scheduler.session_close_failed",
                    conversation_id=raw_conversation_id,
                    exc_info=True,
                )
            else:
                # Session or batch load failed (nothing was sent), or the
                # session's exit raised over a `TurnFailed`: keep its count.
                failure = exc
                delivered_parts = _delivered_before(exc)
        finally:
            await keeper.stop()

        # One more renewal before touching the debounce state: it confirms
        # the lock and gives `finish` a full TTL to run under it.
        if isinstance(result, LockLost) or not await keeper.still_owned():
            logger.warning(
                "scheduler.lock_lost",
                conversation_id=raw_conversation_id,
                delivered_parts=(
                    delivered_parts
                    if failure is not None
                    else getattr(result, "delivered_parts", None)
                ),
                outcome=type(failure if failure is not None else result).__name__,
            )
            if failure is None and isinstance(result, Message):
                # The reply was sent and stored: these ids are answered and
                # must not be answered again. Everything else (due entry,
                # claim, newer in-flight ids) is left to the current holder.
                await debounce.drop_inflight(redis, raw_conversation_id, message_ids)
            return None  # the batch belongs to the current holder (or the claim timeout)
        if failure is not None:
            await _settle_failure(
                redis,
                session_factory,
                raw_conversation_id,
                message_ids,
                failure,
                delivered_parts=delivered_parts,
                now=now,
            )
            return None
        if isinstance(result, Superseded):
            # Put the batch back ahead of the interrupting messages so the
            # next turn answers everything at once.
            await debounce.return_inflight(redis, raw_conversation_id)
            logger.info(
                "scheduler.turn_superseded",
                conversation_id=raw_conversation_id,
                delivered_parts=result.delivered_parts,
            )
        else:
            # Answered or refused on purpose (`None`): the batch is settled,
            # so earlier failed attempts at it no longer count.
            await retry.clear(redis, raw_conversation_id)
            outcome = conversation_id
        # Under the lock: drops in-flight, and either removes the due entry
        # or, if messages arrived meanwhile, makes the conversation due now.
        await debounce.finish(redis, raw_conversation_id)
    finally:
        await lock.release(redis, raw_conversation_id, token)
    return outcome


async def _run_claimed(
    redis: Redis,
    session_factory: SessionFactory,
    runtime: AnyTurnRuntime | None,
    raw_conversation_id: str,
    *,
    now: float,
) -> tuple[uuid.UUID | None, Exception | None]:
    """`_take_turn` that never raises an `Exception`: it is logged and returned.

    Runs as its own task next to the other conversations, so one turn's broken
    bookkeeping cannot cancel or hide the rest. Cancellation still propagates.
    """
    try:
        return await _take_turn(redis, session_factory, runtime, raw_conversation_id, now=now), None
    except Exception as exc:
        logger.exception("scheduler.turn_crashed", conversation_id=raw_conversation_id)
        return None, exc


def _resolve_concurrency(concurrency: int | None) -> int:
    if concurrency is None:
        concurrency = get_settings().worker_concurrency
    if concurrency < 1:
        raise ValueError(f"concurrency must be at least 1, got {concurrency}")
    return concurrency


def _exclusive_sessions(session_factory: SessionFactory) -> SessionFactory:
    """Wrap `session_factory` so no session is ever used by two turns at once.

    `async_sessionmaker` hands out a fresh session per call, and then this
    changes nothing. A factory that hands out one shared session (tests do)
    would otherwise have two concurrent turns on one `AsyncSession`, which is
    not safe: those turns take turns on it instead, in the order they asked.
    Never open a second session from this factory while holding one: with a
    shared session that waits on itself.
    """
    held: dict[int, tuple[asyncio.Lock, int]] = {}

    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        async with session_factory() as session:
            key = id(session)  # stable: the session is alive while held
            guard, users = held.get(key, (asyncio.Lock(), 0))
            held[key] = (guard, users + 1)
            try:
                async with guard:
                    yield session
            finally:
                guard, users = held[key]
                if users <= 1:
                    del held[key]
                else:
                    held[key] = (guard, users - 1)

    return factory


async def tick(
    redis: Redis,
    session_factory: SessionFactory,
    runtime: AnyTurnRuntime | None = None,
    *,
    now: float | None = None,
    limit: int = DUE_BATCH_SIZE,
    concurrency: int | None = None,
) -> list[uuid.UUID]:
    """Process every conversation whose debounce window closed by `now`.

    Up to `concurrency` (default `Settings.worker_concurrency`) conversations
    run at once, each with its own session, and `tick` returns once all of
    them are done. A conversation is claimed only when a slot is free.

    Returns the conversation ids this call actually took a turn for, in due
    order. A turn that fails is settled inside (retry or dead-letter) and left
    out. An error from the Redis bookkeeping itself is logged
    (`scheduler.turn_crashed`) and does not stop the other conversations;
    once they are all done, the first such error (in due order) is raised.
    """
    now = time.time() if now is None else now
    slots = _resolve_concurrency(concurrency)
    sessions = _exclusive_sessions(session_factory)
    due = await debounce.due_conversations(redis, now=now, limit=limit)

    free = asyncio.Semaphore(slots)
    tasks: dict[str, asyncio.Task[Any]] = {}
    errors: dict[str, Exception] = {}
    async with asyncio.TaskGroup() as group:
        for raw_conversation_id in due:
            await free.acquire()
            try:
                claimed = await debounce.claim(redis, raw_conversation_id, now=now)
            except Exception as exc:
                # Redis is failing: claim nothing more this tick, but let the
                # running turns finish.
                free.release()
                logger.exception("scheduler.turn_crashed", conversation_id=raw_conversation_id)
                errors[raw_conversation_id] = exc
                break
            if not claimed:
                free.release()
                continue  # another worker took it, or it is no longer due
            task = group.create_task(
                _run_claimed(redis, sessions, runtime, raw_conversation_id, now=now),
                name=f"{TURN_TASK_PREFIX}{raw_conversation_id}",
            )
            task.add_done_callback(lambda _task: free.release())
            tasks[raw_conversation_id] = task

    processed: list[uuid.UUID] = []
    for raw_conversation_id in due:
        finished = tasks.get(raw_conversation_id)
        if finished is None:
            continue
        conversation_id, error = finished.result()
        if error is not None:
            errors.setdefault(raw_conversation_id, error)
        elif conversation_id is not None:
            processed.append(conversation_id)
    for raw_conversation_id in due:
        if raw_conversation_id in errors:
            raise errors[raw_conversation_id]
    return processed


async def _wait_for_stop(stop: asyncio.Event, timeout: float) -> None:
    """Wait until `stop` is set, at most `timeout` seconds (not `asyncio.sleep`)."""
    try:
        async with asyncio.timeout(timeout):
            await stop.wait()
    except TimeoutError:
        pass


async def _fill_slots(
    redis: Redis,
    session_factory: SessionFactory,
    runtime: AnyTurnRuntime | None,
    running: dict[str, asyncio.Task[Any]],
    slots: int,
    stop: asyncio.Event,
) -> None:
    """Claim and start due conversations while there is a free slot.

    A conversation is claimed only when it can start at once; `stop` is
    checked before every claim.
    """
    if len(running) >= slots or stop.is_set():
        return
    now = time.time()
    for raw_conversation_id in await debounce.due_conversations(
        redis, now=now, limit=DUE_BATCH_SIZE
    ):
        if len(running) >= slots or stop.is_set():
            return
        if raw_conversation_id in running:
            continue  # ours, finishing up; never twice at once
        if not await debounce.claim(redis, raw_conversation_id, now=now):
            continue  # another worker took it, or it is no longer due
        task = asyncio.create_task(
            _run_claimed(redis, session_factory, runtime, raw_conversation_id, now=now),
            name=f"{TURN_TASK_PREFIX}{raw_conversation_id}",
        )
        running[raw_conversation_id] = task
        task.add_done_callback(
            lambda _task, raw_conversation_id=raw_conversation_id: running.pop(  # type: ignore[misc]  # mypy cannot infer a lambda with a default-bound argument
                raw_conversation_id, None
            )
        )


async def run_scheduler(
    redis: Redis,
    session_factory: SessionFactory,
    runtime: AnyTurnRuntime | None = None,
    interval: float = TICK_INTERVAL_SECONDS,
    *,
    concurrency: int | None = None,
    stop: asyncio.Event | None = None,
) -> None:
    """Keep up to `concurrency` turns running until `stop` is set.

    Every `interval` it fills the free slots with due conversations, without
    waiting for the turns already running. Once `stop` is set it claims
    nothing new, waits for the running turns (they finish and store their
    replies) and returns. If it is cancelled instead (the worker's shutdown
    grace ran out), it cancels the running turns and waits for them: each
    releases its lock, and its batch comes back through the claim timeout.
    """
    slots = _resolve_concurrency(concurrency)
    stop = asyncio.Event() if stop is None else stop
    sessions = _exclusive_sessions(session_factory)
    running: dict[str, asyncio.Task[Any]] = {}
    logger.info("scheduler.starting", interval=interval, concurrency=slots)
    try:
        while not stop.is_set():
            try:
                await _fill_slots(redis, sessions, runtime, running, slots, stop)
            except Exception:  # a Redis blip must not kill the scheduler loop
                logger.exception("scheduler.tick_failed")
            await _wait_for_stop(stop, interval)
        logger.info("scheduler.stopping", running=len(running))
        if running:
            await asyncio.wait(list(running.values()))
    finally:
        leftover = [task for task in running.values() if not task.done()]
        for task in leftover:
            task.cancel()
        if leftover:
            await asyncio.gather(*leftover, return_exceptions=True)
            logger.warning("scheduler.turns_cancelled", count=len(leftover))
    logger.info("scheduler.stopped")
