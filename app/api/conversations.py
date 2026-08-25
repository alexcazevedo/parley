"""REST for conversations: read/poll, plus the human-handoff surface — an
operator posting a message, and the handoff/resume/close transitions
(`app.handoff.service.HandoffService` owns the state machine; this router
only translates its `IllegalTransitionError` into a `409`).
"""

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.channels.base import OutgoingMessage
from app.channels.registry import get_adapter
from app.handoff.service import HandoffService, IllegalTransitionError
from app.models.channel import Channel
from app.models.contact import Contact
from app.models.conversation import ConversationState
from app.models.handoff import HandoffRequestedBy
from app.models.message import Message, MessageDirection, MessageRole
from app.models.tenant import Tenant
from app.repositories.conversations import ConversationsRepository
from app.repositories.messages import MessagesRepository

router = APIRouter(prefix="/conversations", tags=["conversations"])

# `Message.type` values that carry the agent's tool-orchestration bookkeeping
# rather than something a human should read — see `AgentRuntime._persist_tool_*`.
_TOOL_MESSAGE_TYPES = {"tool_call", "tool_result"}


class ConversationOut(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    channel_id: uuid.UUID
    agent_id: uuid.UUID
    state: ConversationState
    summary: str | None
    last_message_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    id: uuid.UUID
    conversation_id: uuid.UUID
    channel_id: uuid.UUID
    direction: MessageDirection
    role: MessageRole
    type: str
    content: str
    external_message_id: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class MessageCreate(BaseModel):
    content: str = Field(min_length=1)


class HandoffCreate(BaseModel):
    reason: str | None = None


def _repo(db: AsyncSession) -> ConversationsRepository:
    return ConversationsRepository(db)


async def _get_conversation_or_404(db: AsyncSession, tenant: Tenant, conversation_id: uuid.UUID):
    conversation = await _repo(db).get(tenant.id, conversation_id)
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found"
        )
    return conversation


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    state: ConversationState | None = Query(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[ConversationOut]:
    filters = {"state": state} if state is not None else {}
    conversations = await _repo(db).list(tenant.id, **filters)
    return [ConversationOut.model_validate(c) for c in conversations]


@router.get("/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    conversation_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ConversationOut:
    conversation = await _get_conversation_or_404(db, tenant, conversation_id)
    return ConversationOut.model_validate(conversation)


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_conversation_messages(
    conversation_id: uuid.UUID,
    after: datetime | None = Query(default=None),
    include_tools: bool = Query(default=False),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[MessageOut]:
    await _get_conversation_or_404(db, tenant, conversation_id)
    messages = await MessagesRepository(db).list_by_conversation(tenant.id, conversation_id)
    if after is not None:
        messages = [m for m in messages if m.created_at > after]
    if not include_tools:
        messages = [
            m
            for m in messages
            if m.role != MessageRole.TOOL and m.type not in _TOOL_MESSAGE_TYPES
        ]
    return [MessageOut.model_validate(m) for m in messages]


@router.post(
    "/{conversation_id}/messages", response_model=MessageOut, status_code=status.HTTP_201_CREATED
)
async def post_operator_message(
    conversation_id: uuid.UUID,
    payload: MessageCreate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> MessageOut:
    """An operator's reply while the conversation is (typically) in
    `HUMAN_HANDOFF` — sent through the same `ChannelAdapter` the AI uses, so
    the contact sees it wherever they already are.
    """
    conversation = await _get_conversation_or_404(db, tenant, conversation_id)
    message = Message(
        id=uuid.uuid4(),
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        channel_id=conversation.channel_id,
        direction=MessageDirection.OUT,
        role=MessageRole.HUMAN_OPERATOR,
        type="text",
        content=payload.content,
        created_at=datetime.now(UTC),
    )
    db.add(message)
    await db.flush()

    channel = await db.get(Channel, conversation.channel_id)
    contact = await db.get(Contact, conversation.contact_id)
    assert channel is not None and contact is not None, "conversation has dangling FKs"
    adapter = get_adapter(channel.type)
    await adapter.send_message(
        OutgoingMessage(conversation_id=conversation.id, content=payload.content), contact, channel
    )

    conversation.last_message_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(message)
    return MessageOut.model_validate(message)


@router.post("/{conversation_id}/handoff", response_model=ConversationOut)
async def handoff_conversation(
    conversation_id: uuid.UUID,
    payload: HandoffCreate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ConversationOut:
    conversation = await _get_conversation_or_404(db, tenant, conversation_id)
    try:
        await HandoffService(db).handoff(conversation, payload.reason, HandoffRequestedBy.OPERATOR)
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(conversation)
    return ConversationOut.model_validate(conversation)


@router.post("/{conversation_id}/resume", response_model=ConversationOut)
async def resume_conversation(
    conversation_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ConversationOut:
    conversation = await _get_conversation_or_404(db, tenant, conversation_id)
    try:
        await HandoffService(db).resume(conversation)
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(conversation)
    return ConversationOut.model_validate(conversation)


@router.post("/{conversation_id}/close", response_model=ConversationOut)
async def close_conversation(
    conversation_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ConversationOut:
    conversation = await _get_conversation_or_404(db, tenant, conversation_id)
    try:
        await HandoffService(db).close(conversation)
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.commit()
    await db.refresh(conversation)
    return ConversationOut.model_validate(conversation)
