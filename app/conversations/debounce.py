"""Per-conversation debounce state in Redis.

Keys per conversation:

- ``harness:pending:{conv_id}`` — LIST of message ids waiting for a turn.
- ``harness:inflight:{conv_id}`` — LIST of message ids a claimed turn is
  working on (drained from pending, not yet finished).
- ``harness:claim:{conv_id}`` (`claim_key`) — STRING held by the worker that
  claimed the conversation, with a TTL of the claim timeout.
- ``harness:due`` — ZSET conv_id -> unix ts of the earliest allowed turn
  (one set for all conversations).

Every inbound job pushes its message id and raises the due score to
``now + debounce_seconds`` (`ZADD GT`), so a burst of messages keeps extending
the window and collapses into a single turn. `GT` never pulls a score earlier:
a message that lands while a turn runs leaves the claim's score alone, and
`finish` makes the conversation due when it sees pending messages.

Claiming (`claim`) reads the due score and, if it is due, takes the claim key
with `SET NX PX`: exactly one caller wins, and only the winner then moves the
due score `CLAIM_TIMEOUT_SECONDS` out. Nothing is WATCHed, so writes to the
due set for other conversations never make a claim retry. While the turn runs,
`extend_claim` pushes both the score and the key's TTL out; `finish` deletes
the key. A claimer that dies leaves the key to expire after the timeout.

Recoverability (PRODUCT.md E3.3): from the moment a message id is pushed until
its turn finished it lives in exactly one of the two lists, and the
conversation keeps a due entry. `claim` does not remove that entry, it moves it
into the future; `finish` is the only thing that removes it. A worker that
dies mid-turn therefore leaves a conversation that becomes due again by
itself (score and claim key both run out), and the next turn drains the dead
worker's in-flight batch first.

Everything that must be atomic is a single command or a (WATCH/)MULTI
transaction, not Lua: the test Redis (fakeredis without `lupa`) cannot run
EVAL.
"""

import time
import uuid
from typing import Any, cast

from redis.asyncio import Redis
from redis.exceptions import WatchError

DUE_KEY = "harness:due"
PENDING_KEY_PREFIX = "harness:pending:"
INFLIGHT_KEY_PREFIX = "harness:inflight:"
CLAIM_KEY_PREFIX = "harness:claim:"
# How long a claimed conversation stays off the due list before another worker
# may take it again (its claimer is presumed dead).
CLAIM_TIMEOUT_SECONDS = 60.0


def pending_key(conversation_id: uuid.UUID | str) -> str:
    return f"{PENDING_KEY_PREFIX}{conversation_id}"


def inflight_key(conversation_id: uuid.UUID | str) -> str:
    return f"{INFLIGHT_KEY_PREFIX}{conversation_id}"


def claim_key(conversation_id: uuid.UUID | str) -> str:
    return f"{CLAIM_KEY_PREFIX}{conversation_id}"


def _timeout_ms(timeout: float) -> int:
    return max(1, int(timeout * 1000))


def _to_uuids(items: list[Any]) -> list[uuid.UUID]:
    """Decode list items to ids, each id once at its first position.

    A job can be delivered twice (worker reaped while only slow), and a batch
    can be drained again after a crash: duplicates are harmless here.
    """
    ids = [uuid.UUID(item if isinstance(item, str) else item.decode()) for item in items]
    return list(dict.fromkeys(ids))


async def push_pending(
    redis: Redis,
    conversation_id: uuid.UUID | str,
    message_id: uuid.UUID | str,
    due_at: float,
) -> None:
    """Queue a message for the conversation and (re)arm its debounce window.

    `ZADD GT`: the score only moves later (or is created), so a message that
    lands while a turn runs cannot make the conversation due before the
    claim's score; `finish` makes it due once that turn ends.
    """
    await redis.rpush(pending_key(conversation_id), str(message_id))
    await redis.zadd(DUE_KEY, {str(conversation_id): due_at}, gt=True)


async def due_conversations(redis: Redis, now: float, limit: int = 50) -> list[str]:
    """Conversation ids whose debounce window has elapsed."""
    return list(
        cast(list[str], await redis.zrangebyscore(DUE_KEY, "-inf", now, start=0, num=limit))
    )


async def claim(
    redis: Redis,
    conversation_id: uuid.UUID | str,
    *,
    now: float | None = None,
    timeout: float = CLAIM_TIMEOUT_SECONDS,
) -> bool:
    """Take a due conversation: hold its claim key and move its score out.

    False, writing nothing, unless the entry exists with a score at or before
    `now` *and* this call wins `SET claim_key NX PX timeout`; exactly one
    concurrent caller wins that. Only the winner then moves the score to
    `now + timeout` (`XX`: an entry a finishing turn just removed is not
    brought back). The entry stays, so a claimer that dies leaves the
    conversation due again once the score and the key run out.
    """
    now = time.time() if now is None else now
    member = str(conversation_id)
    score = await redis.zscore(DUE_KEY, member)
    if score is None or score > now:
        return False
    if not await redis.set(claim_key(conversation_id), "1", nx=True, px=_timeout_ms(timeout)):
        return False  # another worker holds the claim
    await redis.zadd(DUE_KEY, {member: now + timeout}, xx=True)
    return True


async def extend_claim(
    redis: Redis,
    conversation_id: uuid.UUID | str,
    *,
    now: float | None = None,
    timeout: float = CLAIM_TIMEOUT_SECONDS,
) -> None:
    """Keep a running turn's claim: due entry to `now + timeout`, key TTL to `timeout`.

    The scheduler's lock heartbeat calls this, so a turn that runs longer than
    the claim timeout is not handed to another worker. One MULTI; `ZADD XX`
    and `PEXPIRE` only touch what exists: an entry or key `finish` already
    removed is not brought back.
    """
    now = time.time() if now is None else now
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zadd(DUE_KEY, {str(conversation_id): now + timeout}, xx=True)
        pipe.pexpire(claim_key(conversation_id), _timeout_ms(timeout))
        await pipe.execute()


async def schedule(redis: Redis, conversation_id: uuid.UUID | str, due_at: float) -> None:
    """Set the due score to `due_at`, earlier or later. Leaves the claim key alone."""
    await redis.zadd(DUE_KEY, {str(conversation_id): due_at})


async def release_claim(
    redis: Redis, conversation_id: uuid.UUID | str, due_at: float
) -> None:
    """Give up a claim and make the conversation due at `due_at`, in one MULTI.

    For a claimer that will not finish the turn now (lock busy, a failed turn
    to retry later): without dropping the key, the conversation could not be
    claimed again before the key's TTL ran out, and its past-due entry would
    sit at the head of the due set meanwhile. The batch stays where it is.
    """
    async with redis.pipeline(transaction=True) as pipe:
        pipe.zadd(DUE_KEY, {str(conversation_id): due_at})
        pipe.delete(claim_key(conversation_id))
        await pipe.execute()


async def drop_inflight(
    redis: Redis, conversation_id: uuid.UUID | str, message_ids: list[uuid.UUID]
) -> None:
    """Take these ids (every copy) off the in-flight list; nothing else.

    For a batch that was answered (reply sent and stored) by a turn that then
    lost the lock: the due entry, the claim key and any other in-flight ids
    belong to whoever holds the conversation now, but these must not be
    answered again. One MULTI.
    """
    if not message_ids:
        return
    inflight = inflight_key(conversation_id)
    async with redis.pipeline(transaction=True) as pipe:
        for message_id in message_ids:
            pipe.lrem(inflight, 0, str(message_id))
        await pipe.execute()


async def drain_pending(redis: Redis, conversation_id: uuid.UUID | str) -> list[uuid.UUID]:
    """Move the pending list to the tail of the in-flight list; return in-flight.

    One transaction: a message is never in neither list, and one arriving
    between the read and the move aborts and retries it. The return value is
    the whole in-flight list (each id once, order kept), so a batch a dead
    worker left in flight comes back ahead of newer messages.
    """
    pending = pending_key(conversation_id)
    inflight = inflight_key(conversation_id)
    async with redis.pipeline(transaction=True) as pipe:
        while True:
            try:
                await pipe.watch(pending, inflight)
                items = await pipe.lrange(pending, 0, -1)
                cast(Any, pipe).multi()
                if items:
                    pipe.rpush(inflight, *items)
                pipe.delete(pending)
                pipe.lrange(inflight, 0, -1)
                results = await pipe.execute()
                return _to_uuids(results[-1])
            except WatchError:
                continue


async def has_pending(redis: Redis, conversation_id: uuid.UUID | str) -> bool:
    return await redis.llen(pending_key(conversation_id)) > 0


async def finish(redis: Redis, conversation_id: uuid.UUID | str) -> None:
    """End a turn: drop the in-flight list and claim key, settle the due entry.

    Nothing pending -> the conversation leaves the due set. Something pending
    -> it is due now. WATCHing the pending list makes the decision atomic
    against `push_pending` (RPUSH then ZADD): a push landing after the read
    aborts the transaction, and one landing after the EXEC still does its own
    ZADD — pending is never left non-empty without a due entry.
    """
    member = str(conversation_id)
    pending = pending_key(conversation_id)
    async with redis.pipeline(transaction=True) as pipe:
        while True:
            try:
                await pipe.watch(pending)
                waiting = await pipe.llen(pending)
                cast(Any, pipe).multi()
                pipe.delete(inflight_key(conversation_id), claim_key(conversation_id))
                if waiting:
                    pipe.zadd(DUE_KEY, {member: time.time()})
                else:
                    pipe.zrem(DUE_KEY, member)
                await pipe.execute()
                return
            except WatchError:
                continue


async def return_inflight(redis: Redis, conversation_id: uuid.UUID | str) -> None:
    """Put the in-flight batch back at the head of the pending list (barge-in).

    Original order, ahead of whatever arrived meanwhile, in one transaction.
    `LPUSH` prepends each argument in turn, so pushing the ids in reverse
    leaves them at the head in their original order. A concurrent RPUSH onto
    pending cannot reorder that; the WATCH is on the in-flight list.
    """
    pending = pending_key(conversation_id)
    inflight = inflight_key(conversation_id)
    async with redis.pipeline(transaction=True) as pipe:
        while True:
            try:
                await pipe.watch(inflight)
                items = await pipe.lrange(inflight, 0, -1)
                if not items:
                    await cast(Any, pipe).unwatch()
                    return
                ids = [str(mid) for mid in _to_uuids(items)]
                cast(Any, pipe).multi()
                pipe.lpush(pending, *reversed(ids))
                pipe.delete(inflight)
                await pipe.execute()
                return
            except WatchError:
                continue
