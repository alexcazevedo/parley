"""Per-conversation advisory lock (`harness:lock:{conv_id}`).

`SET key <token> NX PX` to acquire; release is a compare-and-delete on the
token, so a worker whose lock already expired can never delete the lock a
different worker now holds.

The compare-and-delete is a WATCH/MULTI transaction rather than the more usual
Lua script: fakeredis only speaks EVAL when the optional `lupa` dependency is
installed, and adding it is outside this ticket. The semantics are the same —
the DEL only lands if the token is untouched between GET and EXEC.
"""

import uuid

from redis.asyncio import Redis
from redis.exceptions import WatchError

LOCK_KEY_PREFIX = "harness:lock:"
LOCK_TTL_MS = 60_000


def lock_key(conversation_id: uuid.UUID | str) -> str:
    return f"{LOCK_KEY_PREFIX}{conversation_id}"


async def acquire(
    redis: Redis, conversation_id: uuid.UUID | str, ttl_ms: int = LOCK_TTL_MS
) -> str | None:
    """Return the lock token on success, `None` if someone else holds it."""
    token = uuid.uuid4().hex
    acquired = await redis.set(lock_key(conversation_id), token, nx=True, px=ttl_ms)
    return token if acquired else None


async def release(redis: Redis, conversation_id: uuid.UUID | str, token: str) -> bool:
    """Release only if we still own the lock. True if it was ours."""
    key = lock_key(conversation_id)
    async with redis.pipeline(transaction=True) as pipe:
        while True:
            try:
                await pipe.watch(key)
                current = await pipe.get(key)
                if current != token:
                    await pipe.unwatch()
                    return False
                pipe.multi()
                pipe.delete(key)
                await pipe.execute()
                return True
            except WatchError:
                continue  # someone touched the key; re-read and re-check
