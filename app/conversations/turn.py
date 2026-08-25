"""Turn execution: one debounced batch of inbound messages -> one reply.

`process_turn` is deliberately runtime-agnostic: it joins the batch into a
single `input_text`, hands it to a runtime, then persists the answer as an
outbound assistant message and pushes it through the channel adapter.

Two runtime shapes are accepted. `TurnRuntime` is text-in/text-out and needs
nothing from the database (`EchoRuntime`, test doubles).
`SessionAwareTurnRuntime` additionally receives the turn's session, because it
writes its own rows — `app.agents.runtime.AgentRuntime` persists an `AgentRun`
plus the tool-call/tool-result messages and must land in the same transaction
as the reply. The worker injects `AgentRuntime`; `EchoRuntime` stays the
default so nothing accidentally calls a live model.
"""

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import ChannelAdapter, OutgoingMessage
from app.channels.registry import get_adapter
from app.config import get_settings
from app.conversations.humanize import split_reply, typing_delay_seconds
from app.conversations.summary import maybe_summarize
from app.llm.factory import get_provider
from app.models.agent import Agent
from app.models.channel import Channel
from app.models.contact import Contact
from app.models.conversation import Conversation, ConversationState
from app.models.message import Message, MessageDirection, MessageRole

logger = structlog.get_logger(__name__)


@runtime_checkable
class TurnRuntime(Protocol):
    """What a turn needs from an agent implementation: text in, text out."""

    async def run(self, conversation: Conversation, input_text: str) -> str: ...


@runtime_checkable
class SessionAwareTurnRuntime(Protocol):
    """A runtime that persists rows of its own and so needs the turn's session."""

    async def run_turn(
        self, session: AsyncSession, conversation: Conversation, input_text: str
    ) -> str: ...


AnyTurnRuntime = TurnRuntime | SessionAwareTurnRuntime


class EchoRuntime:
    """Default runtime until the LLM runtime lands — echoes the input back."""

    async def run(self, conversation: Conversation, input_text: str) -> str:
        return f"echo: {input_text}"


async def process_turn(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    messages: list[Message],
    runtime: AnyTurnRuntime | None = None,
) -> Message | None:
    """Run one turn for `conversation_id` over `messages` (arrival order).

    Returns the persisted assistant message, or `None` when no reply was
    produced (unknown conversation, empty batch, or a conversation that is not
    `ACTIVE_AI` — a human is driving it, so the AI stays quiet).
    """
    if not messages:
        return None

    conversation = await session.get(Conversation, conversation_id)
    if conversation is None:
        logger.warning("turn.unknown_conversation", conversation_id=str(conversation_id))
        return None

    if conversation.state != ConversationState.ACTIVE_AI:
        logger.info(
            "turn.skipped_not_active_ai",
            conversation_id=str(conversation_id),
            state=str(conversation.state),
        )
        return None

    # Loaded before the runtime runs: `humanize` decides whether the inbound
    # batch gets a read receipt *before* we start "typing".
    channel = await session.get(Channel, conversation.channel_id)
    contact = await session.get(Contact, conversation.contact_id)
    assert channel is not None and contact is not None, "conversation has dangling FKs"
    adapter = get_adapter(channel.type)
    humanized = bool((channel.config or {}).get("humanize"))

    if humanized:
        await _mark_read(adapter, messages, contact, channel)

    input_text = "\n".join(message.content for message in messages)
    runtime = runtime or EchoRuntime()
    if isinstance(runtime, SessionAwareTurnRuntime):
        reply_text = await runtime.run_turn(session, conversation, input_text)
    else:
        reply_text = await runtime.run(conversation, input_text)

    if humanized:
        reply = await _deliver_humanized(
            session, conversation, adapter, contact, channel, reply_text
        )
    else:
        reply = _build_reply(conversation, reply_text)
        session.add(reply)
        await session.flush()
        await adapter.send_message(
            OutgoingMessage(conversation_id=conversation.id, content=reply_text),
            contact,
            channel,
        )

    conversation.last_message_at = datetime.now(UTC)
    await _maybe_summarize(session, conversation, runtime)
    await session.commit()
    logger.info(
        "turn.completed",
        conversation_id=str(conversation_id),
        message_count=len(messages),
    )
    return reply


def _build_reply(conversation: Conversation, content: str) -> Message:
    return Message(
        id=uuid.uuid4(),
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        channel_id=conversation.channel_id,
        direction=MessageDirection.OUT,
        role=MessageRole.ASSISTANT,
        type="text",
        content=content,
        # Explicit timestamp: Postgres `now()` is the transaction timestamp, so
        # a server default would order the reply *before* the tool messages the
        # runtime just wrote inside this same transaction.
        created_at=datetime.now(UTC),
    )


async def _mark_read(
    adapter: ChannelAdapter, messages: list[Message], contact: Contact, channel: Channel
) -> None:
    """Best-effort read receipt for the newest inbound message of the batch."""
    external_message_id = messages[-1].external_message_id
    if not external_message_id:
        return
    try:
        await adapter.mark_read(external_message_id, contact, channel)
    except Exception:
        logger.warning("turn.mark_read_failed", channel_id=str(channel.id), exc_info=True)


async def _deliver_humanized(
    session: AsyncSession,
    conversation: Conversation,
    adapter: ChannelAdapter,
    contact: Contact,
    channel: Channel,
    reply_text: str,
) -> Message:
    """Split the reply and deliver it as paced bubbles, committing per part.

    The commit inside the loop is the point: polling clients (webchat) see each
    bubble appear as it is sent instead of the whole reply at once. It also
    commits whatever the runtime wrote (AgentRun, tool messages) — fine, the
    turn is past the point where it would roll back.
    """
    settings = get_settings()
    parts = split_reply(reply_text, settings.humanize_max_parts) or [reply_text]

    last: Message | None = None
    for index, part in enumerate(parts):
        delay = typing_delay_seconds(part, settings)
        try:
            await adapter.send_typing(contact, channel, delay)
        except Exception:
            logger.warning("turn.send_typing_failed", channel_id=str(channel.id), exc_info=True)
        await asyncio.sleep(delay)

        message = _build_reply(conversation, part)
        session.add(message)
        await session.flush()
        try:
            await adapter.send_message(
                OutgoingMessage(conversation_id=conversation.id, content=part), contact, channel
            )
        except Exception:
            # The first part keeps the non-humanized path's behavior (raise, turn
            # fails); later parts are best-effort so an already-delivered reply
            # is never rolled back over a partial send failure.
            if index == 0:
                raise
            logger.warning("turn.part_send_failed", channel_id=str(channel.id), exc_info=True)
        await session.commit()
        last = message

    assert last is not None, "split_reply fallback guarantees at least one part"
    return last


async def _maybe_summarize(
    session: AsyncSession, conversation: Conversation, runtime: AnyTurnRuntime
) -> None:
    """Best-effort rolling summary: never allowed to cost the user their reply.

    Reuses `runtime.llm_provider(agent)` when the runtime exposes one (e.g.
    `AgentRuntime`, so the exact same — possibly scripted — provider the turn
    just used is reused); falls back to `app.llm.factory.get_provider`
    otherwise. `TurnRuntime` implementations with no LLM at all (e.g.
    `EchoRuntime`) are simply skipped.
    """
    try:
        agent = await session.get(Agent, conversation.agent_id)
        if agent is None:
            return
        provider_for = getattr(runtime, "llm_provider", None)
        llm = provider_for(agent) if callable(provider_for) else get_provider(agent)
        await maybe_summarize(session, agent, conversation, llm)
    except Exception:
        logger.exception("turn.summarize_failed", conversation_id=str(conversation.id))
