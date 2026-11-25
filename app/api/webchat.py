"""Web Chat REST ingestion endpoint. The client polls
`GET /conversations/{id}/messages` for replies — there is no push transport.

`ingest_message`/`resolve_channel` are re-exported and reused by
`app/api/dev.py` so the dev simulator funnels through the exact same code
path (see PRODUCT.md: "it never bypasses the pipeline").
"""

import uuid
from collections.abc import Callable, Coroutine
from typing import Any

import structlog
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.routing import APIRoute
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.types import Message

from app.api.deps import get_current_tenant, get_db, get_redis
from app.channels.base import IncomingMedia
from app.channels.registry import get_adapter
from app.config import get_settings
from app.media.sniff import media_type_for, sniff_mime
from app.messages.ingest import MediaRejected, ingest
from app.models.channel import Channel, ChannelType
from app.models.tenant import Tenant
from app.queue.redis_queue import RedisQueue
from app.repositories.channels import ChannelsRepository
from app.security.limits import check_message_size
from app.security.ratelimit import enforce_inbound_limits

logger = structlog.get_logger(__name__)

_CHUNK_SIZE = 64 * 1024
_MULTIPART_OVERHEAD = 1_048_576


class CappedBodyRoute(APIRoute):
    """Cap the request body of one route by bytes actually received. A
    `Content-Length` over the media cap plus multipart overhead is a 413
    before the body is read; regardless of the header, the bytes of every
    `http.request` message are counted and the request is cut off with a 413
    once the total passes the cap.
    """

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            limit = get_settings().media_max_bytes + _MULTIPART_OVERHEAD
            if request.method == "POST":
                raw = request.headers.get("content-length")
                if raw is not None and raw.strip().isdigit() and int(raw) > limit:
                    raise _too_large()

            inner = request.receive
            received = 0

            async def counting_receive() -> Message:
                nonlocal received
                message = await inner()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > limit:
                        raise _too_large()
                return message

            request._receive = counting_receive
            return await original(request)

        return handler


def _too_large() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
        detail="request body too large",
    )


router = APIRouter(prefix="/channels/webchat", tags=["webchat"])
media_router = APIRouter(route_class=CappedBodyRoute)


class WebChatMessageIn(BaseModel):
    channel_id: uuid.UUID
    user_id: str
    message: str
    message_id: str | None = None


class IngestOut(BaseModel):
    conversation_id: uuid.UUID
    message_id: uuid.UUID
    duplicate: bool


_NOT_OF_TYPE = {
    ChannelType.WEBCHAT: "channel is not a webchat channel",
    ChannelType.EXTERNAL: "channel is not an external channel",
}


async def resolve_channel(
    db: AsyncSession,
    tenant: Tenant,
    channel_id: uuid.UUID,
    channel_type: ChannelType = ChannelType.WEBCHAT,
) -> Channel:
    """The tenant's channel `channel_id`: 404 when missing or another tenant's,
    422 when it is not of `channel_type`."""
    channel = await ChannelsRepository(db).get(tenant.id, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    if channel.type != channel_type:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_NOT_OF_TYPE.get(channel_type, f"channel is not a {channel_type} channel"),
        )
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


async def ingest_text_message(
    db: AsyncSession,
    redis: Redis,
    tenant: Tenant,
    channel: Channel,
    user_id: str,
    message: str,
    message_id: str | None = None,
) -> IngestOut:
    """A text message posted by a client: inbound limits, size limit (413), ingest."""
    await enforce_inbound_limits(redis, tenant.id, channel.id, user_id)

    # Size limit: strict enforcement (413 if exceeded)
    within_limit, _message = check_message_size(message, enforce_strict=True)
    if not within_limit:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    return await ingest_message(db, redis, tenant, channel, user_id, message, message_id)


@router.post("/messages", response_model=IngestOut, status_code=status.HTTP_201_CREATED)
async def post_webchat_message(
    payload: WebChatMessageIn,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> IngestOut:
    channel = await resolve_channel(db, tenant, payload.channel_id)
    return await ingest_text_message(
        db, redis, tenant, channel, payload.user_id, payload.message, payload.message_id
    )


async def receive_media_upload(
    db: AsyncSession,
    redis: Redis,
    tenant: Tenant,
    channel: Channel,
    user_id: str,
    file: UploadFile,
    caption: str | None,
    message_id: str | None,
    *,
    allow_documents: bool = False,
) -> IngestOut:
    """A file posted by a client: inbound limits, caption size (413), the body
    read up to `media_max_bytes` (413), the type decided from the file's
    content (magic bytes; the client's content type is ignored), ingest. Unknown
    content is a 415, and so is a PDF unless `allow_documents`.
    """
    await enforce_inbound_limits(redis, tenant.id, channel.id, user_id)

    caption_text = caption or ""
    within_limit, _message = check_message_size(caption_text, enforce_strict=True)
    if not within_limit:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    max_bytes = get_settings().media_max_bytes
    buffer = bytearray()
    while chunk := await file.read(_CHUNK_SIZE):
        buffer.extend(chunk)
        if len(buffer) > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="media exceeds max size",
            )
    data = bytes(buffer)

    sniffed = sniff_mime(data)
    media_type = media_type_for(sniffed) if sniffed is not None else None
    if sniffed is None or (media_type == "document" and not allow_documents):
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="unsupported media content",
        )
    media = IncomingMedia(data=data, mime=sniffed, filename=file.filename)

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


@media_router.post(
    "/media",
    response_model=IngestOut,
    status_code=status.HTTP_201_CREATED,
)
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
    """Upload path for `/chat`'s attach-a-photo/voice-note flow (images and
    audio; a PDF is a 415)."""
    channel = await resolve_channel(db, tenant, channel_id)
    return await receive_media_upload(
        db, redis, tenant, channel, user_id, file, caption, message_id
    )


router.include_router(media_router)
