"""Dev simulator: `POST /dev/messages` is a thin wrapper over the exact same
ingestion pipeline as Web Chat (`app/api/webchat.py`). It exists to script
demo scenarios without a real widget — it never bypasses the pipeline.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db, get_redis
from app.api.webchat import IngestOut, ingest_message, resolve_channel
from app.models.tenant import Tenant
from app.security.limits import check_message_size
from app.security.ratelimit import check_rate_limit

router = APIRouter(prefix="/dev", tags=["dev"])


class DevMessageIn(BaseModel):
    channel_id: uuid.UUID
    user_id: str
    message: str


@router.post("/messages", response_model=IngestOut, status_code=status.HTTP_201_CREATED)
async def post_dev_message(
    payload: DevMessageIn,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> IngestOut:
    channel = await resolve_channel(db, tenant, payload.channel_id)

    # Rate limit: channel_id:user_id
    from app.config import get_settings
    settings = get_settings()
    user_key = f"{payload.channel_id}:{payload.user_id}"
    await check_rate_limit(redis, "user", user_key, settings.rate_limit_user_per_min)

    # Size limit: strict enforcement (413 if exceeded)
    within_limit, _message = check_message_size(
        payload.message, enforce_strict=True
    )
    if not within_limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        )

    return await ingest_message(db, redis, tenant, channel, payload.user_id, payload.message)
