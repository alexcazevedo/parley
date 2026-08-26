"""Web Chat REST ingestion endpoint. The client polls
`GET /conversations/{id}/messages` for replies — there is no push transport.

`ingest_message`/`resolve_channel` are re-exported and reused by
`app/api/dev.py` so the dev simulator funnels through the exact same code
path (see PRODUCT.md: "it never bypasses the pipeline").
"""

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db, get_redis
from app.channels.base import IncomingMedia
from app.channels.registry import get_adapter
from app.messages.ingest import MediaRejected, ingest
from app.models.channel import Channel
from app.models.tenant import Tenant
from app.queue.redis_queue import RedisQueue
from app.repositories.channels import ChannelsRepository
from app.security.limits import check_message_size
from app.security.ratelimit import check_rate_limit

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/channels/webchat", tags=["webchat"])


class WebChatMessageIn(BaseModel):
    channel_id: uuid.UUID
    user_id: str
    message: str
    message_id: str | None = None


class IngestOut(BaseModel):
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    duplicate: bool


async def resolve_channel(db: AsyncSession, tenant: Tenant, channel_id: uuid.UUID) -> Channel:
    channel = await ChannelsRepository(db).get(tenant.id, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    return channel


async def ingest_message(
    db: AsyncSession,
    redis: Redis,
    tenant: Tenant,
    channel: Channel,
    user_id: str,
    message: str,
    message_id: str | None = None,
    media: IncomingMedia | None = None,
    media_type: str | None = None,
) -> IngestOut:
    try:
        adapter = get_adapter(channel.type)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    raw: dict[str, Any] = {
        "tenant_id": str(tenant.id),
        "channel_id": str(channel.id),
        "user_id": user_id,
        "message": message,
        "message_id": message_id or str(uuid.uuid4()),
    }
    if media is not None:
        raw["media"] = media
        raw["type"] = media_type
    incoming = adapter.parse_incoming(raw)

    try:
        result = await ingest(db, RedisQueue(redis), incoming)
    except MediaRejected as exc:
        await db.rollback()
        status_code = (
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
            if "size" in exc.reason
            else status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
        )
        raise HTTPException(status_code=status_code, detail=exc.reason) from exc

    await db.commit()
    return IngestOut(
        conversation_id=result.conversation_id,
        message_id=result.message_id,
        duplicate=result.duplicate,
    )


@router.post("/messages", response_model=IngestOut, status_code=status.HTTP_201_CREATED)
async def post_webchat_message(
    payload: WebChatMessageIn,
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

    return await ingest_message(
        db, redis, tenant, channel, payload.user_id, payload.message, payload.message_id
    )


@router.post("/media", response_model=IngestOut, status_code=status.HTTP_201_CREATED)
async def post_webchat_media(
    channel_id: uuid.UUID = Form(...),
    user_id: str = Form(...),
    file: UploadFile = File(...),
    caption: str | None = Form(default=None),
    message_id: str | None = Form(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> IngestOut:
    """Upload path for `/chat`'s attach-a-photo/voice-note flow. Type is
    derived from the file's content-type, not client-supplied — `image/*` /
    `audio/*` map to `image`/`audio`, anything else is a 415.
    """
    channel = await resolve_channel(db, tenant, channel_id)

    # Rate limit: channel_id:user_id (same scope as the text endpoint)
    from app.config import get_settings
    settings = get_settings()
    user_key = f"{channel_id}:{user_id}"
    await check_rate_limit(redis, "user", user_key, settings.rate_limit_user_per_min)

    content_type = (file.content_type or "").split(";", 1)[0].strip()
    if content_type.startswith("image/"):
        media_type = "image"
    elif content_type.startswith("audio/"):
        media_type = "audio"
    else:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"unsupported content type: {content_type!r}",
        )

    caption_text = caption or ""
    within_limit, _message = check_message_size(caption_text, enforce_strict=True)
    if not within_limit:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    data = await file.read()
    media = IncomingMedia(data=data, mime=content_type, filename=file.filename)

    return await ingest_message(
        db,
        redis,
        tenant,
        channel,
        user_id,
        caption_text,
        message_id,
        media=media,
        media_type=media_type,
    )
