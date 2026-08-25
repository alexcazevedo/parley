"""Web Chat adapter: REST in, REST poll out — there is no push transport, so
`send_message` only persists (the client polls
`GET /conversations/{id}/messages`).
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.channels.base import ChannelAdapter, IncomingMessage, OutgoingMessage
from app.models.channel import Channel
from app.models.contact import Contact


class WebChatAdapter(ChannelAdapter):
    def parse_incoming(self, payload: dict[str, Any]) -> IncomingMessage:
        return IncomingMessage(
            tenant_id=UUID(str(payload["tenant_id"])),
            channel_id=UUID(str(payload["channel_id"])),
            external_user_id=str(payload["user_id"]),
            external_message_id=str(payload["message_id"]),
            type="text",
            content=payload["message"],
            timestamp=datetime.now(UTC),
            raw=payload,
        )

    async def send_message(
        self, message: OutgoingMessage, contact: Contact, channel: Channel
    ) -> None:
        # Persistence happens in the caller (worker/turn execution); Web Chat
        # has no server-initiated push, the client polls for replies.
        return None
