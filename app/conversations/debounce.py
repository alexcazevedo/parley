"""Per-conversation debounce state in Redis.

Two keys per conversation:

- ``harness:pending:{conv_id}`` — LIST of message ids waiting for a turn.
- ``harness:due`` — ZSET conv_id -> unix ts of the earliest allowed turn.

Every inbound job pushes its message id and *overwrites* the due score with
``now + debounce_seconds``, so a burst of messages keeps extending the window
and collapses into a single turn.
"""

import uuid

from redis.asyncio import Redis

DUE_KEY = "harness:due"
PENDING_KEY_PREFIX = "harness:pending:"



def pending_key(conversation_id: uuid.UUID | str) -> str:
    return f"{PENDING_KEY_PREFIX}{conversation_id}"


async def push_pending(
    redis: Redis,
    conversation_id: uuid.UUID | str,
    message_id: uuid.UUID | str,
    due_at: float,
) -> None:
    """Queue a message for the conversation and (re)arm its debounce window."""
    await redis.rpush(pending_key(conversation_id), str(message_id))
    await redis.zadd(DUE_KEY, {str(conversation_id): due_at})


async def due_conversations(redis: Redis, now: float, limit: int = 50) -> list[str]:
    """Conversation ids whose debounce window has elapsed."""
    return list(
        await redis.zrangebyscore(DUE_KEY, "-inf", now, start=0, num=limit)
    )


async def claim(redis: Redis, conversation_id: uuid.UUID | str) -> bool:
    """Remove the conversation from the due set; only one caller gets True."""
    return await redis.zrem(DUE_KEY, str(conversation_id)) == 1


async def schedule(redis: Redis, conversation_id: uuid.UUID | str, due_at: float) -> None:
    await redis.zadd(DUE_KEY, {str(conversation_id): due_at})


async def drain_pending(redis: Redis, conversation_id: uuid.UUID | str) -> list[uuid.UUID]:
    """Take the whole pending batch and clear it in one MULTI/EXEC.

    Atomicity matters: a message arriving between the LRANGE and the DEL would
    otherwise be silently dropped.
    """
    key = pending_key(conversation_id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.lrange(key, 0, -1)
        pipe.delete(key)
        items, _ = await pipe.execute()
    return [uuid.UUID(item if isinstance(item, str) else item.decode()) for item in items]


async def has_pending(redis: Redis, conversation_id: uuid.UUID | str) -> bool:
    return await redis.llen(pending_key(conversation_id)) > 0


async def requeue_pending(
    redis: Redis,
    conversation_id: uuid.UUID | str,
    message_ids: list[uuid.UUID],
) -> None:
    """Put a drained batch back at the head of the pending list (barge-in).

    Used when a turn is superseded: `message_ids` (the batch that was just
    drained and processed) needs to go back in front of whatever arrived
    while the turn was running, so the next turn answers old + new together,
    in arrival order. `LPUSH` prepends each argument in turn, so pushing the
    ids in reverse order leaves them at the head in their original order —
    ahead of the interrupting messages already on the list.
    """
    if not message_ids:
        return
    key = pending_key(conversation_id)
    await redis.lpush(key, *[str(mid) for mid in reversed(message_ids)])
