"""Inbound rate limiting: per-user (channel_id:user_id) first, then tenant-wide.

Fixed one-minute windows in Redis. Key format: `harness:rl:{scope}:{key}:{minute}`.
Scope: "tenant" (key = tenant_id) or "user" (key = "channel_id:user_id").
INCR and EXPIRE run in one MULTI/EXEC pipeline so a counter can never be
left without a TTL.
"""

import time
import uuid

from fastapi import HTTPException, status
from redis.asyncio import Redis

from app.config import get_settings

_KEY_TTL_SECONDS = 120


async def hit_rate_limit(redis: Redis, scope: str, key: str, limit: int) -> bool:
    """Count one hit; return True when the window's count exceeds `limit`."""
    minute = int(time.time()) // 60
    rl_key = f"harness:rl:{scope}:{key}:{minute}"

    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(rl_key)
        pipe.expire(rl_key, _KEY_TTL_SECONDS)
        current, _ = await pipe.execute()

    return int(current) > limit


async def check_rate_limit(redis: Redis, scope: str, key: str, limit: int) -> None:
    """Check rate limit. Raises 429 if exceeded."""
    if await hit_rate_limit(redis, scope, key, limit):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS)


async def inbound_over_limit(
    redis: Redis, tenant_id: uuid.UUID | str, channel_id: uuid.UUID | str, user_id: str
) -> bool:
    """Per-user limit first, then tenant-wide. True when either is exceeded.

    A user already over their own limit returns early without counting a
    tenant hit, so one noisy user cannot use up the tenant's budget.
    """
    settings = get_settings()
    if await hit_rate_limit(
        redis, "user", f"{channel_id}:{user_id}", settings.rate_limit_user_per_min
    ):
        return True
    return await hit_rate_limit(redis, "tenant", str(tenant_id), settings.rate_limit_tenant_per_min)


async def enforce_inbound_limits(
    redis: Redis, tenant_id: uuid.UUID | str, channel_id: uuid.UUID | str, user_id: str
) -> None:
    """Raise 429 when the tenant or user inbound limit is exceeded."""
    if await inbound_over_limit(redis, tenant_id, channel_id, user_id):
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS)
