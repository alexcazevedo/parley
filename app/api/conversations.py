"""Read-only REST for conversations, plus polling for their messages (the Web
Chat client has no push transport). Mutation endpoints (post message, resume)
land in a later ticket — see PRODUCT.md's success demo.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.models.conversation import ConversationState
from app.models.message import MessageDirection, MessageRole
from app.models.tenant import Tenant
from app.repositories.conversations import ConversationsRepository
from app.repositories.messages import MessagesRepository

router = APIRouter(prefix="/conversations", tags=["conversations"])


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


def _repo(db: AsyncSession) -> ConversationsRepository:
    return ConversationsRepository(db)


@router.get("", response_model=list[ConversationOut])
async def list_conversations(
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[ConversationOut]:
    conversations = await _repo(db).list(tenant.id)
    return [ConversationOut.model_validate(c) for c in conversations]


@router.get("/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    conversation_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ConversationOut:
    conversation = await _repo(db).get(tenant.id, conversation_id)
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found"
        )
    return ConversationOut.model_validate(conversation)


@router.get("/{conversation_id}/messages", response_model=list[MessageOut])
async def list_conversation_messages(
    conversation_id: uuid.UUID,
    after: datetime | None = Query(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[MessageOut]:
    conversation = await _repo(db).get(tenant.id, conversation_id)
    if conversation is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="conversation not found"
        )
    messages = await MessagesRepository(db).list_by_conversation(tenant.id, conversation_id)
    if after is not None:
        messages = [m for m in messages if m.created_at > after]
    return [MessageOut.model_validate(m) for m in messages]
