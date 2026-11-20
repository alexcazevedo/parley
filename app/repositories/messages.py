"""Messages repository. Ingestion/queueing lands in a later ticket — this only
provides the tenant-scoped reads/writes that repository will need.
"""

import uuid

from sqlalchemy import select

from app.models.message import Message
from app.repositories.base import BaseRepository


class MessagesRepository(BaseRepository[Message]):
    model = Message

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
