"""Health endpoints.

`/health` is liveness: no DB/Redis dependency on purpose, cheap for
orchestrators. `/ready` is readiness: it checks that the database and Redis
both answer.
"""

import asyncio
from typing import Any

import structlog
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis

router = APIRouter(tags=["health"])
log = structlog.get_logger()

CHECK_TIMEOUT = 2.0


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def _check(name: str, coro: Any) -> str:
    try:
        await asyncio.wait_for(coro, timeout=CHECK_TIMEOUT)
    except Exception as exc:  # includes TimeoutError
        log.warning("ready.check_failed", check=name, error=repr(exc))
        return "failed"
    return "ok"


@router.get("/ready")
async def ready(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> JSONResponse:
    database = await _check("database", db.execute(text("SELECT 1")))
    redis_status = await _check("redis", redis.ping())
    checks = {"database": database, "redis": redis_status}
    all_ok = all(v == "ok" for v in checks.values())
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content={"status": "ready" if all_ok else "not_ready", "checks": checks},
    )
