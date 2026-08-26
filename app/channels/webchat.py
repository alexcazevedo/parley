"""Web Chat adapter: REST in, REST poll out — there is no push transport, so
`send_message` only persists (the client polls
`GET /conversations/{id}/messages`).
"""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from app.channels.base import ChannelAdapter, IncomingMedia, IncomingMessage, OutgoingMessage
from app.models.channel import Channel
from app.models.contact import Contact


class WebChatAdapter(ChannelAdapter):
    def parse_incoming(self, payload: dict[str, Any]) -> IncomingMessage:
        media: IncomingMedia | None = payload.get("media")
        msg_type = payload.get("type", "text")
        content = payload.get("message") or ""

        # `raw_payload` is stored as JSONB — never put the media bytes in
        # there, just its mime/filename.
        raw = {k: v for k, v in payload.items() if k != "media"}
        if media is not None:
            raw["media"] = {"mime": media.mime, "filename": media.filename}

        return IncomingMessage(
            tenant_id=UUID(str(payload["tenant_id"])),
            channel_id=UUID(str(payload["channel_id"])),
            external_user_id=str(payload["user_id"]),
            external_message_id=str(payload["message_id"]),
            type=msg_type,
            content=content,
            timestamp=datetime.now(UTC),
            raw=raw,
            media=media,
        )

    async def send_message(
        self, message: OutgoingMessage, contact: Contact, channel: Channel
    ) -> None:
        # Persistence happens in the caller (worker/turn execution); Web Chat
        # has no server-initiated push, the client polls for replies.
        return None
