"""Channel-specific inbound webhooks.

Unlike Web Chat / dev (`app/api/webchat.py`, `app/api/dev.py`), these are
authenticated per-channel (a header secret compared against the channel's
`secret_encrypted`), not per-tenant (`X-API-Key`) — the tenant is resolved
from the channel row, not from a header, so `get_current_tenant` is never
used here.
"""

import secrets as pysecrets
import uuid
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis
from app.channels.evolution import EvolutionAdapter, IgnoredEvent
from app.messages.ingest import find_existing, ingest
from app.models.channel import Channel
from app.queue.redis_queue import RedisQueue
from app.security import secrets as secrets_crypto
from app.security.limits import check_message_size
from app.security.ratelimit import inbound_over_limit

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = structlog.get_logger(__name__)


@router.post("/whatsapp/{channel_id}", status_code=status.HTTP_200_OK)
async def whatsapp_webhook(
    channel_id: uuid.UUID,
    payload: dict[str, Any],
    apikey: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, str]:
    result = await db.execute(select(Channel).where(Channel.id == channel_id))
    channel = result.scalar_one_or_none()
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")

    expected = (
        secrets_crypto.decrypt(channel.secret_encrypted) if channel.secret_encrypted else None
    )
    if not expected or not apikey or not pysecrets.compare_digest(apikey, expected):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid apikey")

    adapter = EvolutionAdapter()
    # Identity always comes from the authenticated channel row; payload keys
    # are spread first so they can never override it.
    raw: dict[str, Any] = {
        **payload,
        "tenant_id": str(channel.tenant_id),
        "channel_id": str(channel.id),
    }
    try:
        incoming = adapter.parse_incoming(raw)
    except IgnoredEvent as exc:
        logger.info("webhook.event_ignored", reason=str(exc))
        return {"status": "ignored"}

    # A repeated delivery is recognised before any other work: no rate-limit
    # count, no write, no job (PRODUCT.md E4.4).
    if await find_existing(db, incoming) is not None:
        return {"status": "ok"}

    # Over the inbound limit: never 429 (Evolution would drop or retry the
    # message). Store it so operators see it, but enqueue no job, so no agent
    # turn runs; its media is never downloaded.
    rate_limited = await inbound_over_limit(
        redis, channel.tenant_id, channel.id, incoming.external_user_id
    )
    if rate_limited and incoming.type in ("image", "audio"):
        incoming.type = "unsupported"
        incoming.content = "[mídia não recebida]"
    # The webhook downloads nothing: an image or audio row is stored with its
    # type and `media_key` null, and the worker fetches the bytes right before
    # the turn (`app/media/fetch.py`). `parse_incoming` already stripped any
    # inline base64 from `incoming.raw`.
    incoming.media = None

    # Size limit: silently truncate and log for webhooks
    within_limit, message_to_store = check_message_size(
        incoming.content, enforce_strict=False
    )
    if not within_limit:
        logger.warning(
            "webhook.message_truncated",
            channel_id=str(channel_id),
            external_message_id=incoming.external_message_id,
        )
        incoming.content = message_to_store

    if rate_limited:
        await ingest(db, RedisQueue(redis), incoming, enqueue=False)
        await db.commit()
        logger.warning(
            "webhook.rate_limited",
            tenant_id=str(channel.tenant_id),
            channel_id=str(channel_id),
            external_message_id=incoming.external_message_id,
        )
        return {"status": "rate_limited"}

    await ingest(db, RedisQueue(redis), incoming)
    await db.commit()
    return {"status": "ok"}
