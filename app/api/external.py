"""External channel endpoints: the project forwards its customers' messages
(`POST /channels/external/messages`) and reads the agent's side as an ordered
event feed (`GET /channels/{channel_id}/events`).

Ingestion is the web chat path (`ingest_text_message`): same limits, dedupe
on `message_id` and response. The feed is paged by `seq`: a reader keeps the
last `seq` it handled and asks for `after=<seq>`; events of a channel are
inserted under its advisory lock (`app/events/outbox.py`), so none appears
behind a position already read.
"""

import uuid
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db, get_redis
from app.api.pagination import DEFAULT_LIMIT, MAX_LIMIT
from app.api.webchat import (
    CappedBodyRoute,
    IngestOut,
    ingest_text_message,
    receive_media_upload,
    resolve_channel,
)
from app.channels.registry import get_adapter
from app.conversations.resolver import resolve_contact_and_conversation
from app.events.outbox import CAUSE_STAFF_MESSAGE
from app.handoff.service import HandoffService
from app.messages.ingest import find_existing
from app.models.channel import Channel, ChannelType
from app.models.channel_event import ChannelEvent
from app.models.conversation import Conversation, ConversationState
from app.models.handoff import HandoffRequestedBy
from app.models.message import Message, MessageDirection, MessageRole
from app.models.tenant import Tenant
from app.security.limits import check_message_size
from app.security.ratelimit import enforce_inbound_limits

router = APIRouter(tags=["external"])
media_router = APIRouter(route_class=CappedBodyRoute)


class ExternalMessageIn(BaseModel):
    channel_id: uuid.UUID
    user_id: str
    message: str
    message_id: str | None = None
    # `staff`: a message from the project's own people to the customer. It
    # pauses the agent for the conversation (see `_ingest_staff_message`).
    sender: Literal["customer", "staff"] = "customer"
    staff_name: str | None = Field(default=None, max_length=120)


class ChannelEventOut(BaseModel):
    id: uuid.UUID
    seq: int
    type: str
    conversation_id: uuid.UUID
    created_at: datetime
    data: dict[str, Any]

    model_config = {"from_attributes": True}


@router.post(
    "/channels/external/messages",
    response_model=IngestOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_external_message(
    payload: ExternalMessageIn,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> IngestOut:
    channel = await resolve_channel(db, tenant, payload.channel_id, ChannelType.EXTERNAL)
    if payload.sender == "staff":
        return await _ingest_staff_message(db, redis, tenant, channel, payload)
    return await ingest_text_message(
        db, redis, tenant, channel, payload.user_id, payload.message, payload.message_id
    )


@media_router.post(
    "/channels/external/media",
    response_model=IngestOut,
    status_code=status.HTTP_201_CREATED,
)
async def post_external_media(
    channel_id: uuid.UUID = Form(...),
    user_id: str = Form(...),
    file: UploadFile = File(...),
    caption: str | None = Form(default=None),
    message_id: str | None = Form(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> IngestOut:
    """An image, voice note or PDF of the customer: the web chat upload, and a
    PDF (`document`) is accepted too."""
    channel = await resolve_channel(db, tenant, channel_id, ChannelType.EXTERNAL)
    return await receive_media_upload(
        db, redis, tenant, channel, user_id, file, caption, message_id, allow_documents=True
    )


async def _ingest_staff_message(
    db: AsyncSession, redis: Redis, tenant: Tenant, channel: Channel, payload: ExternalMessageIn
) -> IngestOut:
    """Store a staff message like an operator's (out, `human_operator`), start no
    turn, record no `message.reply`. An `ACTIVE_AI` conversation moves to
    `HUMAN_HANDOFF` through `HandoffService` (events recorded there, same
    transaction); a customer with no open conversation gets one that way too.
    A repeated `message_id` stores nothing and changes nothing.
    """
    await enforce_inbound_limits(redis, tenant.id, channel.id, payload.user_id)
    within_limit, _message = check_message_size(payload.message, enforce_strict=True)
    if not within_limit:
        raise HTTPException(status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE)

    raw: dict[str, Any] = {
        "tenant_id": str(tenant.id),
        "channel_id": str(channel.id),
        "user_id": payload.user_id,
        "message": payload.message,
        "message_id": payload.message_id or str(uuid.uuid4()),
    }
    incoming = get_adapter(channel.type).parse_incoming(raw)
    incoming.raw = {**incoming.raw, "sender": "staff", "staff_name": payload.staff_name}

    _contact, conversation = await resolve_contact_and_conversation(db, incoming)
    # Locked until the commit, and re-read: the state decides the handoff below.
    await db.execute(
        select(Conversation.id)
        .where(Conversation.id == conversation.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    await db.refresh(conversation)

    inserted_id = (
        await db.execute(
            insert(Message)
            .values(
                id=uuid.uuid4(),
                tenant_id=tenant.id,
                conversation_id=conversation.id,
                channel_id=channel.id,
                direction=MessageDirection.OUT,
                role=MessageRole.HUMAN_OPERATOR,
                type="text",
                content=incoming.content,
                external_message_id=incoming.external_message_id,
                raw_payload=incoming.raw,
            )
            .on_conflict_do_nothing(
                index_elements=["tenant_id", "channel_id", "external_message_id"],
                index_where=text("external_message_id IS NOT NULL"),
            )
            .returning(Message.id)
        )
    ).scalar_one_or_none()
    if inserted_id is None:
        existing_id = await find_existing(db, incoming)
        assert existing_id is not None
        await db.commit()
        return IngestOut(conversation_id=conversation.id, message_id=existing_id, duplicate=True)

    conversation.last_message_at = datetime.now(UTC)
    if conversation.state == ConversationState.ACTIVE_AI:
        await HandoffService(db).handoff(
            conversation,
            "staff message",
            HandoffRequestedBy.OPERATOR,
            cause=CAUSE_STAFF_MESSAGE,
        )
    conversation_id = conversation.id
    await db.commit()
    return IngestOut(conversation_id=conversation_id, message_id=inserted_id, duplicate=False)


router.include_router(media_router)


@router.get("/channels/{channel_id}/events", response_model=list[ChannelEventOut])
async def list_channel_events(
    channel_id: uuid.UUID,
    after: int = Query(default=0, ge=0),
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[ChannelEventOut]:
    channel = await resolve_channel(db, tenant, channel_id, ChannelType.EXTERNAL)
    rows = await db.execute(
        select(ChannelEvent)
        .where(
            ChannelEvent.tenant_id == tenant.id,
            ChannelEvent.channel_id == channel.id,
            ChannelEvent.seq > after,
        )
        .order_by(ChannelEvent.seq)
        .limit(limit)
    )
    return [ChannelEventOut.model_validate(event) for event in rows.scalars().all()]
