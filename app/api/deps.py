"""Shared FastAPI dependencies re-exported for convenience.

`app/main.py` overrides `get_db` / `get_redis` in tests via
`app.dependency_overrides`, so routers should depend on these names
rather than constructing sessions/clients themselves.
"""

from redis.asyncio import Redis

from app.config import get_settings
from app.db import get_db  # noqa: F401  (re-exported)
from app.security.auth import get_current_tenant, require_admin  # noqa: F401

_redis_client: Redis | None = None


async def get_redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        _redis_client = Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client
