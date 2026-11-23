"""Messages repository. Ingestion/queueing lands in a later ticket — this only
provides the tenant-scoped reads/writes that repository will need.
"""

import uuid
from datetime import datetime

from sqlalchemy import select

from app.models.message import Message, MessageRole
from app.repositories.base import BaseRepository

# `Message.type` values that carry tool-orchestration bookkeeping.
TOOL_MESSAGE_TYPES = ("tool_call", "tool_result")


class MessagesRepository(BaseRepository[Message]):
    model = Message

    async def page_by_conversation(
        self,
        tenant_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        after_seq: int | None = None,
        before_seq: int | None = None,
        after: datetime | None = None,
        include_tools: bool = False,
        limit: int,
        newest: bool = False,
    ) -> list[Message]:
        """One SELECT, filtered in SQL, at most `limit + 1` rows (the extra row
        tells the caller more remain). Always returned ascending by `seq`;
        `newest=True` takes the newest rows instead of the oldest.
        """
        stmt = select(Message).where(
            Message.tenant_id == tenant_id, Message.conversation_id == conversation_id
        )
        if after_seq is not None:
            stmt = stmt.where(Message.seq > after_seq)
        if before_seq is not None:
            stmt = stmt.where(Message.seq < before_seq)
        if after is not None:
            stmt = stmt.where(Message.created_at > after)
        if not include_tools:
            stmt = stmt.where(
                Message.role != MessageRole.TOOL, Message.type.not_in(TOOL_MESSAGE_TYPES)
            )
        stmt = stmt.order_by(Message.seq.desc() if newest else Message.seq).limit(limit + 1)
        rows = list((await self.session.execute(stmt)).scalars().all())
        if newest:
            rows.reverse()
        return rows

    async def list_by_conversation(
        self, tenant_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> list[Message]:
        result = await self.session.execute(
            select(Message)
            .where(Message.tenant_id == tenant_id, Message.conversation_id == conversation_id)
            .order_by(Message.seq)
        )
        return list(result.scalars().all())

    async def get_by_external_message_id(
        self, tenant_id: uuid.UUID, channel_id: uuid.UUID, external_message_id: str
    ) -> Message | None:
        result = await self.session.execute(
            select(Message).where(
                Message.tenant_id == tenant_id,
                Message.channel_id == channel_id,
                Message.external_message_id == external_message_id,
            )
        )
        return result.scalar_one_or_none()
