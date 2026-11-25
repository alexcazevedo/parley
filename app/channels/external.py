"""External adapter: a channel the project owns. The project forwards its
customers' messages (`POST /channels/external/messages`) and reads the
agent's replies from the channel's event feed (`GET /channels/{id}/events`),
so `send_message` sends nothing: the turn records a `message.reply` event in
the same transaction as the reply row (`app/events/outbox.py`).
"""

from typing import Any

from app.channels.base import IncomingMessage, OutgoingMessage
from app.channels.webchat import WebChatAdapter
from app.models.channel import Channel
from app.models.contact import Contact


class ExternalAdapter(WebChatAdapter):
    def parse_incoming(self, payload: dict[str, Any]) -> IncomingMessage:
        return super().parse_incoming(payload)

    async def send_message(
        self, message: OutgoingMessage, contact: Contact, channel: Channel
    ) -> None:
        return None
