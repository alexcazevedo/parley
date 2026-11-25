"""The event outbox of `external` channels.

`record_event` inserts one `ChannelEvent` in the caller's transaction, so an
event exists exactly when the row it describes (a reply) is committed. Feed
readers page by `seq > after`; `seq` is taken at insert, not at commit, so an
event inserted earlier but committed later could be skipped. Inserts of one
channel are therefore serialized with a transaction-scoped advisory lock keyed
on the channel: within a channel, insert order is commit order.
"""

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.channel import Channel, ChannelType
from app.models.channel_event import ChannelEvent
from app.models.conversation import Conversation

MESSAGE_REPLY = "message.reply"
CONVERSATION_HANDOFF = "conversation.handoff"  # {"reason", "requested_by", "brief"}
CONVERSATION_HANDOFF_BRIEF = "conversation.handoff_brief"  # {"brief"}
CONVERSATION_AGENT = "conversation.agent"  # {"active": bool, "cause": CAUSE_*}
CONVERSATION_CATEGORY = "conversation.category"  # {"category_id", "name", "source"}

# `cause` of a `conversation.agent` event.
CAUSE_STAFF_MESSAGE = "staff_message"
CAUSE_AGENT_HANDOFF = "agent_handoff"
CAUSE_SENTIMENT = "sentiment"
CAUSE_OPERATOR = "operator"
CAUSE_RESUMED = "resumed"
CAUSE_CLOSED = "closed"


def channel_lock_key(channel_id: uuid.UUID) -> int:
    """The advisory lock key of a channel's feed: its id's first 8 bytes, signed."""
    return int.from_bytes(channel_id.bytes[:8], "big", signed=True)


async def external_channel(session: AsyncSession, conversation: Conversation) -> Channel | None:
    """The conversation's channel when it is an `external` one of its tenant, else `None`.

    `session.get`: no query when the channel is already loaded (the turn's is).
    """
    channel = await session.get(Channel, conversation.channel_id)
    if (
        channel is None
        or channel.tenant_id != conversation.tenant_id
        or channel.type != ChannelType.EXTERNAL
    ):
        return None
    return channel


async def record_event(
    session: AsyncSession, conversation: Conversation, type: str, data: dict[str, Any]
) -> None:
    """Add a `type` event for `conversation` to its channel's feed, uncommitted.

    Does nothing when the conversation's channel is not `external`. Takes the
    channel's advisory lock (held until the caller's transaction ends), then
    inserts and flushes the row.
    """
    channel = await external_channel(session, conversation)
    if channel is None:
        return
    await session.execute(select(func.pg_advisory_xact_lock(channel_lock_key(channel.id))))
    session.add(
        ChannelEvent(
            id=uuid.uuid4(),
            tenant_id=conversation.tenant_id,
            channel_id=channel.id,
            conversation_id=conversation.id,
            type=type,
            data=data,
        )
    )
    await session.flush()
