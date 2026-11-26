"""Failed-turn bookkeeping in Redis (PRODUCT.md E3.2).

Two keys:

- ``harness:turn_attempts:{conv_id}`` — STRING counter of failed attempts at
  the conversation's current batch. Expires `ATTEMPTS_TTL_SECONDS` after the
  last failure, so a counter left behind (a crash between a failure and its
  handling) cannot follow the conversation forever.
- ``harness:dlq:turns`` (`TURN_DLQ_KEY`) — LIST of dead-lettered batches, one
  JSON object per entry, oldest first: ``conversation_id``, ``message_ids``,
  ``error``, ``partly_delivered``, ``attempts``, ``failed_at`` (unix seconds).

The scheduler decides what to do with a failure; this module only counts and
records. No Lua: the test Redis (fakeredis without `lupa`) cannot run EVAL, so
the counter's INCR + EXPIRE go out as one MULTI/EXEC.
"""

import json
import time
import uuid
from collections.abc import Iterable

from redis.asyncio import Redis

# Attempts per batch, the first one included.
MAX_TURN_ATTEMPTS = 3
# Wait before attempt n+1 after attempt n failed: RETRY_BACKOFF_SECONDS[n-1].
RETRY_BACKOFF_SECONDS = (2.0, 8.0)
TURN_DLQ_KEY = "harness:dlq:turns"
ATTEMPTS_KEY_PREFIX = "harness:turn_attempts:"
ATTEMPTS_TTL_SECONDS = 3600


def attempts_key(conversation_id: uuid.UUID | str) -> str:
    return f"{ATTEMPTS_KEY_PREFIX}{conversation_id}"


async def record_failure(redis: Redis, conversation_id: uuid.UUID | str) -> int:
    """Count one more failed attempt; return the count, this one included."""
    key = attempts_key(conversation_id)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, ATTEMPTS_TTL_SECONDS)
        count, _ = await pipe.execute()
    return int(count)


async def clear(redis: Redis, conversation_id: uuid.UUID | str) -> None:
    """Forget the conversation's failed attempts (its batch is settled)."""
    await redis.delete(attempts_key(conversation_id))


async def dead_letter(
    redis: Redis,
    conversation_id: uuid.UUID | str,
    message_ids: Iterable[uuid.UUID | str],
    *,
    error: str,
    partly_delivered: bool,
    attempts: int,
) -> None:
    """Append one entry for a batch no further attempt will be made for."""
    entry = {
        "conversation_id": str(conversation_id),
        "message_ids": [str(message_id) for message_id in message_ids],
        "error": error,
        "partly_delivered": partly_delivered,
        "attempts": attempts,
        "failed_at": time.time(),
    }
    await redis.rpush(TURN_DLQ_KEY, json.dumps(entry))
