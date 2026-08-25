"""Channel adapter interface.

Every inbound transport (Web Chat REST, WhatsApp/Evolution webhook, ...)
normalizes its payload into an `IncomingMessage` via `parse_incoming`, and
sends replies back out via `send_message`. `app/messages/ingest.py` only
ever talks to this interface, never to a concrete adapter.
"""

import abc
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.models.channel import Channel
from app.models.contact import Contact


@dataclass
class IncomingMessage:
    tenant_id: uuid.UUID
    channel_id: uuid.UUID
    external_user_id: str
    external_message_id: str
    type: str
    content: str
    timestamp: datetime
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OutgoingMessage:
    conversation_id: uuid.UUID
    content: str
    type: str = "text"


class ChannelAdapter(abc.ABC):
    """One implementation per `app.models.channel.ChannelType`."""

    @abc.abstractmethod
    def parse_incoming(self, payload: dict[str, Any]) -> IncomingMessage:
        """Normalize a channel-specific inbound payload."""

    @abc.abstractmethod
    async def send_message(
        self, message: OutgoingMessage, contact: Contact, channel: Channel
    ) -> None:
        """Deliver an outbound message through this channel's transport."""

    async def download_media(self, *args: Any, **kwargs: Any) -> bytes:
        raise NotImplementedError("media is out of scope — text only (see PRODUCT.md)")
