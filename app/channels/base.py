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

# Media MIME types accepted for each `IncomingMessage.type`; a mime with a
# `;codecs=` suffix is matched on the part before `;` (see
# `app/messages/ingest.py`).
MEDIA_TYPES = {"image", "audio"}
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp"}
AUDIO_MIMES = {
    "audio/ogg",
    "audio/mpeg",
    "audio/mp4",
    "audio/webm",
    "audio/wav",
    "audio/x-wav",
    "audio/mp3",
}


@dataclass
class IncomingMedia:
    data: bytes
    mime: str
    filename: str | None = None


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
    media: IncomingMedia | None = None


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

    async def check_endpoint(self, channel: Channel) -> None:
        """Raise `app.tools.ssrf.SSRFError` if this channel's outbound endpoint
        is refused, without calling it.

        `process_turn` asks before any model, tool or speech-to-text call, so a
        refused channel costs nothing. No-op by default: channels without a
        tenant-controlled endpoint have nothing to check.
        """
        return None

    async def mark_read(
        self, external_message_id: str, contact: Contact, channel: Channel
    ) -> None:
        """Mark an inbound message as read on the channel's transport.

        No-op by default; channels that support read receipts override this.
        """
        return None

    async def send_typing(
        self, contact: Contact, channel: Channel, duration_seconds: float
    ) -> None:
        """Show a "typing…"/composing indicator on the channel's transport.

        No-op by default; channels that support presence override this.
        """
        return None
