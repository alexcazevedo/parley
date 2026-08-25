"""Web Chat REST ingestion endpoint. The client polls
`GET /conversations/{id}/messages` for replies — there is no push transport.

`ingest_message`/`resolve_channel` are re-exported and reused by
`app/api/dev.py` so the dev simulator funnels through the exact same code
path (see PRODUCT.md: "it never bypasses the pipeline").
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db, get_redis
from app.channels.registry import get_adapter
from app.messages.ingest import ingest
from app.models.channel import Channel
from app.models.tenant import Tenant
from app.queue.redis_queue import RedisQueue
from app.repositories.channels import ChannelsRepository

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
    incoming = adapter.parse_incoming(raw)

    result = await ingest(db, RedisQueue(redis), incoming)
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
    return await ingest_message(
        db, redis, tenant, channel, payload.user_id, payload.message, payload.message_id
    )
