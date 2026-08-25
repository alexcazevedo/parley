"""Turn execution: one debounced batch of inbound messages -> one reply.

`process_turn` is deliberately runtime-agnostic: it joins the batch into a
single `input_text`, hands it to a `TurnRuntime`, then persists the answer as
an outbound assistant message and pushes it through the channel adapter.
Ticket 04 ships `EchoRuntime`; the real LLM/tool runtime plugs into the same
protocol later.
"""

import uuid
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import OutgoingMessage
from app.channels.registry import get_adapter
from app.models.channel import Channel
from app.models.contact import Contact
from app.models.conversation import Conversation, ConversationState
from app.models.message import Message, MessageDirection, MessageRole

logger = structlog.get_logger(__name__)


@runtime_checkable
class TurnRuntime(Protocol):
    """What a turn needs from an agent implementation: text in, text out."""

    async def run(self, conversation: Conversation, input_text: str) -> str: ...


class EchoRuntime:
    """Default runtime until the LLM runtime lands — echoes the input back."""

    async def run(self, conversation: Conversation, input_text: str) -> str:
        return f"echo: {input_text}"


async def process_turn(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    messages: list[Message],
    runtime: TurnRuntime | None = None,
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

    input_text = "\n".join(message.content for message in messages)
    runtime = runtime or EchoRuntime()
    reply_text = await runtime.run(conversation, input_text)

    reply = Message(
        id=uuid.uuid4(),
        tenant_id=conversation.tenant_id,
        conversation_id=conversation.id,
        channel_id=conversation.channel_id,
        direction=MessageDirection.OUT,
        role=MessageRole.ASSISTANT,
        type="text",
        content=reply_text,
    )
    session.add(reply)
    await session.flush()

    channel = await session.get(Channel, conversation.channel_id)
    contact = await session.get(Contact, conversation.contact_id)
    assert channel is not None and contact is not None, "conversation has dangling FKs"

    adapter = get_adapter(channel.type)
    await adapter.send_message(
        OutgoingMessage(conversation_id=conversation.id, content=reply_text),
        contact,
        channel,
    )

    conversation.last_message_at = datetime.now(UTC)
    await session.commit()
    logger.info(
        "turn.completed",
        conversation_id=str(conversation_id),
        message_count=len(messages),
    )
    return reply
