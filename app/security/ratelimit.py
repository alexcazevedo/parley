"""Rate limiting: per-tenant and per-user (channel_id:user_id).

Uses Redis INCR + EXPIRE pattern: key format is `harness:rl:{scope}:{key}:{minute}`.
Scope: "tenant" or "user". Key: tenant_id or "channel_id:user_id".
"""

import time
from collections.abc import Callable

from fastapi import Depends, HTTPException, status
from redis.asyncio import Redis

from app.api.deps import get_redis
from app.config import get_settings


async def check_rate_limit(
    redis: Redis, scope: str, key: str, limit: int
) -> None:
    """Check rate limit. Raises 429 if exceeded."""
    minute = int(time.time()) // 60
    rl_key = f"harness:rl:{scope}:{key}:{minute}"

    current = await redis.incr(rl_key)
    if current == 1:
        await redis.expire(rl_key, 60)

    if current > limit:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS)


def rate_limit_tenant(limit_override: int | None = None) -> Callable:
    """Rate limit by tenant_id. Use as dependency."""
    async def _check(
        tenant_id: str, redis: Redis = Depends(get_redis)
    ) -> None:
        settings = get_settings()
        limit = limit_override or settings.rate_limit_tenant_per_min
        await check_rate_limit(redis, "tenant", tenant_id, limit)
    return _check


def rate_limit_user(limit_override: int | None = None) -> Callable:
    """Rate limit by channel_id:user_id. Use as dependency."""
    async def _check(
        channel_id: str, user_id: str, redis: Redis = Depends(get_redis)
    ) -> None:
        settings = get_settings()
        limit = limit_override or settings.rate_limit_user_per_min
        key = f"{channel_id}:{user_id}"
        await check_rate_limit(redis, "user", key, limit)
    return _check
