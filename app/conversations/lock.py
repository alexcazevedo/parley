"""Per-conversation advisory lock (`harness:lock:{conv_id}`).

`SET key <token> NX PX` to acquire; release is a compare-and-delete on the
token, so a worker whose lock already expired can never delete the lock a
different worker now holds.

The compare-and-delete is a WATCH/MULTI transaction rather than the more usual
Lua script: fakeredis only speaks EVAL when the optional `lupa` dependency is
installed, and adding it is outside this ticket. The semantics are the same —
the DEL only lands if the token is untouched between GET and EXEC.

A turn can outlast `LOCK_TTL_MS`, so the scheduler `renew`s the lock every
`LOCK_RENEW_INTERVAL_SECONDS` while the turn runs (PRODUCT.md E3.5). `renew` is
the same compare-then-write: it only resets the TTL of a key that still holds
our token, so an expired or taken-over lock is never resurrected.
"""

import uuid

from redis.asyncio import Redis
from redis.exceptions import WatchError

LOCK_KEY_PREFIX = "harness:lock:"
LOCK_TTL_MS = 60_000
# A third of the TTL: two renewals in a row can fail before the lock expires.
LOCK_RENEW_INTERVAL_SECONDS = 20.0


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


async def renew(
    redis: Redis, conversation_id: uuid.UUID | str, token: str, ttl_ms: int = LOCK_TTL_MS
) -> bool:
    """Reset the lock's TTL to `ttl_ms` if we still own it. True if it is ours.

    False when the key is gone (expired) or holds another token; the key is
    then left exactly as it was.
    """
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
                pipe.pexpire(key, ttl_ms)
                (renewed,) = await pipe.execute()
                return bool(renewed)
            except WatchError:
                continue  # someone touched the key; re-read and re-check
