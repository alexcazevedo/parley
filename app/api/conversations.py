"""REST for conversations: read/poll, plus the human-handoff surface — an
operator posting a message, and the handoff/resume/close transitions
(`app.handoff.service.HandoffService` owns the state machine; this router
only translates its `IllegalTransitionError` into a `409`).

An operator's transition is committed at once and never waits for a running
turn (PRODUCT.md E4.2): the handoff endpoint commits the state change first
and only then generates the operator brief (a model call), stored by a second
commit. A brief that fails leaves the handoff in place without one.
"""

import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.channels.base import OutgoingMessage
from app.channels.registry import get_adapter
from app.handoff.service import HandoffService, IllegalTransitionError
from app.handoff.summarizer import generate_handoff_summary
from app.models.channel import Channel
from app.models.contact import Contact
from app.models.conversation import Conversation, ConversationState
from app.models.handoff import Handoff, HandoffRequestedBy
from app.models.message import Message, MessageDirection, MessageRole
from app.models.tenant import Tenant
from app.repositories.conversations import ConversationsRepository
from app.repositories.messages import MessagesRepository
from app.tools.ssrf import SSRFError

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])

# `Message.type` values that carry the agent's tool-orchestration bookkeeping
# rather than something a human should read — see `AgentRuntime._persist_tool_*`.
_TOOL_MESSAGE_TYPES = {"tool_call", "tool_result"}


class HandoffOut(BaseModel):
    """The warm-handoff operator brief — the newest `Handoff` row for a
    conversation, surfaced on the single-conversation GET only (see
    `get_conversation`).
    """

    reason: str | None
    summary: str | None
    requested_by: HandoffRequestedBy
    created_at: datetime

    model_config = {"from_attributes": True}


class ConversationOut(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    channel_id: uuid.UUID
    agent_id: uuid.UUID
    state: ConversationState
    summary: str | None
    last_message_at: datetime | None
    created_at: datetime
    latest_handoff: HandoffOut | None = None
    contact_external_user_id: str | None = None
    contact_display_name: str | None = None

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
    media_url: str | None = None
    media_mime: str | None = None

    model_config = {"from_attributes": True}

    @classmethod
    def from_message(cls, message: Message) -> "MessageOut":
        """`media_url` is derived (not a column) — `/media/{id}` when the
        message has a stored file, `None` (text/tool messages) otherwise.
        """
        out = cls.model_validate(message)
        if message.media_key:
            out.media_url = f"/media/{message.id}"
        return out


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
    state: list[ConversationState] | None = Query(default=None),
    include_handoff: bool = Query(default=False),
    limit: int = Query(default=100, ge=1, le=500),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[ConversationOut]:
    """Powers the operator inbox and Ativas lists: repeatable `state` filter,
    newest-activity-first ordering, contact label always attached (one extra
    statement), and — only when `include_handoff=true` — the latest handoff
    brief per row via a single bounded `DISTINCT ON` statement. With
    `include_handoff` left at its default, `latest_handoff` stays null (its
    default) rather than being fetched per row, which would be an N+1;
    single-GET below always pays that one extra query for the full brief.
    """
    conversations = await _repo(db).list_for_inbox(tenant.id, state, limit)
    outs = [ConversationOut.model_validate(c) for c in conversations]
    await _attach_contacts(db, tenant, conversations, outs)
    if include_handoff:
        await _attach_latest_handoffs(db, tenant, conversations, outs)
    return outs


@router.get("/{conversation_id}", response_model=ConversationOut)
async def get_conversation(
    conversation_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ConversationOut:
    conversation = await _get_conversation_or_404(db, tenant, conversation_id)
    out = ConversationOut.model_validate(conversation)
    await _attach_contacts(db, tenant, [conversation], [out])
    latest_handoff = await _latest_handoff(db, tenant, conversation_id)
    if latest_handoff is not None:
        out.latest_handoff = HandoffOut.model_validate(latest_handoff)
    return out


async def _attach_contacts(
    db: AsyncSession,
    tenant: Tenant,
    conversations: list[Conversation],
    outs: list[ConversationOut],
) -> None:
    """Loads every contact referenced by `conversations` in ONE statement and
    maps `external_user_id`/`display_name` onto the matching `ConversationOut`
    — used by both the list and the single GET so neither pays an N+1.
    """
    contact_ids = {c.contact_id for c in conversations}
    if not contact_ids:
        return
    result = await db.execute(
        select(Contact).where(Contact.tenant_id == tenant.id, Contact.id.in_(contact_ids))
    )
    contacts_by_id = {c.id: c for c in result.scalars().all()}
    for conversation, out in zip(conversations, outs, strict=True):
        contact = contacts_by_id.get(conversation.contact_id)
        if contact is not None:
            out.contact_external_user_id = contact.external_user_id
            out.contact_display_name = contact.display_name


async def _attach_latest_handoffs(
    db: AsyncSession,
    tenant: Tenant,
    conversations: list[Conversation],
    outs: list[ConversationOut],
) -> None:
    """List-sized equivalent of `_latest_handoff`: one `DISTINCT ON
    (conversation_id)` statement for the whole page instead of one query per
    row. Conversations without any handoff keep `latest_handoff=None`.
    """
    conversation_ids = [c.id for c in conversations]
    if not conversation_ids:
        return
    result = await db.execute(
        select(Handoff)
        .distinct(Handoff.conversation_id)
        .where(Handoff.tenant_id == tenant.id, Handoff.conversation_id.in_(conversation_ids))
        .order_by(Handoff.conversation_id, Handoff.created_at.desc())
    )
    handoffs_by_conversation = {h.conversation_id: h for h in result.scalars().all()}
    for conversation, out in zip(conversations, outs, strict=True):
        handoff = handoffs_by_conversation.get(conversation.id)
        if handoff is not None:
            out.latest_handoff = HandoffOut.model_validate(handoff)


async def _latest_handoff(
    db: AsyncSession, tenant: Tenant, conversation_id: uuid.UUID
) -> Handoff | None:
    """Newest handoff for the conversation regardless of resolved state —
    unlike `HandoffService._latest_open_handoff`, this surfaces the brief
    for handoffs the operator has already resumed/closed too.
    """
    result = await db.execute(
        select(Handoff)
        .where(Handoff.tenant_id == tenant.id, Handoff.conversation_id == conversation_id)
        .order_by(Handoff.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


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
    return [MessageOut.from_message(m) for m in messages]


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
    try:
        await adapter.send_message(
            OutgoingMessage(conversation_id=conversation.id, content=payload.content),
            contact,
            channel,
        )
    except SSRFError as exc:
        # The channel's base_url is refused (logged as `channel.endpoint_refused`
        # by the adapter); nothing was sent, so the operator message is dropped.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail="channel endpoint refused"
        ) from exc

    conversation.last_message_at = datetime.now(UTC)
    await db.commit()
    await db.refresh(message)
    return MessageOut.from_message(message)


@router.post("/{conversation_id}/handoff", response_model=ConversationOut)
async def handoff_conversation(
    conversation_id: uuid.UUID,
    payload: HandoffCreate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ConversationOut:
    """Hand the conversation to a human: committed before the brief is written.

    Order: `HandoffService.handoff` and commit (the AI is silenced from here
    on, whatever a running turn does next); then `generate_handoff_summary`
    (best-effort, no transaction open during its model call); then the brief
    is stored on the `Handoff` row by a second commit. Not
    `handoff_with_summary`, which generates the brief before the state change.
    """
    conversation = await _get_conversation_or_404(db, tenant, conversation_id)
    try:
        record = await HandoffService(db).handoff(
            conversation, payload.reason, HandoffRequestedBy.OPERATOR
        )
    except IllegalTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    await db.commit()

    summary = await generate_handoff_summary(db, conversation, payload.reason)
    if summary is not None:
        try:
            record.summary = summary
            await db.commit()
        except Exception:
            # The handoff is committed already; only the brief is lost.
            await db.rollback()
            logger.warning(
                "handoff.summary_not_saved", conversation_id=str(conversation_id), exc_info=True
            )
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
