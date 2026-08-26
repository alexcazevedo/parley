"""Channel-specific inbound webhooks.

Unlike Web Chat / dev (`app/api/webchat.py`, `app/api/dev.py`), these are
authenticated per-channel (a header secret compared against the channel's
`secret_encrypted`), not per-tenant (`X-API-Key`) — the tenant is resolved
from the channel row, not from a header, so `get_current_tenant` is never
used here.
"""

import logging
import secrets as pysecrets
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis
from app.channels.evolution import EvolutionAdapter, IgnoredEvent
from app.config import get_settings
from app.messages.ingest import MediaRejected, ingest
from app.models.channel import Channel
from app.queue.redis_queue import RedisQueue
from app.security import secrets as secrets_crypto
from app.security.limits import check_message_size
from app.security.ratelimit import check_rate_limit

router = APIRouter(prefix="/webhooks", tags=["webhooks"])
logger = logging.getLogger(__name__)


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
    raw: dict[str, Any] = {
        "tenant_id": str(channel.tenant_id),
        "channel_id": str(channel.id),
        **payload,
    }
    try:
        incoming = adapter.parse_incoming(raw)
    except IgnoredEvent as exc:
        logger.info("ignoring evolution webhook event: %s", exc)
        return {"status": "ignored"}

    # Rate limit: channel_id:external_user_id
    settings = get_settings()
    user_key = f"{channel_id}:{incoming.external_user_id}"
    await check_rate_limit(
        redis, "user", user_key, settings.rate_limit_user_per_min
    )

    if incoming.type in ("image", "audio"):
        try:
            incoming.media = await adapter.fetch_media(incoming, channel)
        except Exception:
            logger.exception(
                "webhook.media_fetch_failed: channel_id=%s, external_message_id=%s",
                channel_id,
                incoming.external_message_id,
            )
            incoming.type = "unsupported"
            incoming.content = "[mídia não recebida]"
            incoming.media = None

    # Size limit: silently truncate and log for webhooks
    within_limit, message_to_store = check_message_size(
        incoming.content, enforce_strict=False
    )
    if not within_limit:
        logger.warning(
            "webhook message truncated: channel_id=%s, user_id=%s",
            channel_id,
            incoming.external_user_id,
        )
        incoming.content = message_to_store

    try:
        await ingest(db, RedisQueue(redis), incoming)
    except MediaRejected as exc:
        logger.warning(
            "webhook.media_fetch_failed: channel_id=%s, external_message_id=%s, reason=%s",
            channel_id,
            incoming.external_message_id,
            exc.reason,
        )
        incoming.type = "unsupported"
        incoming.content = "[mídia não recebida]"
        incoming.media = None
        await ingest(db, RedisQueue(redis), incoming)
    await db.commit()
    return {"status": "ok"}
