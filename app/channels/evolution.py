"""WhatsApp channel via Evolution API: webhook ingestion (`messages.upsert`)
and outbound delivery via Evolution's `sendText` REST endpoint.

Channel `config`: `{base_url, instance}`. `secret_encrypted` holds the
Evolution `apikey`, used both to authenticate inbound webhooks (`app/api/
webhooks.py` compares it against the `apikey` header) and as the outbound
`apikey` header on `send_message`.
"""

import base64
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

from app.channels.base import ChannelAdapter, IncomingMedia, IncomingMessage, OutgoingMessage
from app.models.channel import Channel
from app.models.contact import Contact
from app.security import secrets as secrets_crypto

logger = logging.getLogger(__name__)

_JID_SUFFIX = "@s.whatsapp.net"

# Message keys that carry a still-unsupported media type: the content shown
# to the customer/operator, no media fetched (see PRODUCT.md "Out of
# scope" — video/documents/stickers/locations/contacts get only this
# polite fallback, never the STT/vision pipeline).
_UNSUPPORTED_KINDS = {
    "videoMessage": "[vídeo]",
    "documentMessage": "[documento]",
    "stickerMessage": "[figurinha]",
    "locationMessage": "[localização]",
    "contactMessage": "[contato]",
    "contactsArrayMessage": "[contato]",
}


class IgnoredEvent(Exception):
    """Raised by `parse_incoming` for payloads that should be acknowledged
    (HTTP 200) but not persisted: any event other than `messages.upsert`,
    outbound echoes (`key.fromMe`), and non-text messages.
    """


class EvolutionAdapter(ChannelAdapter):
    def parse_incoming(self, payload: dict[str, Any]) -> IncomingMessage:
        if payload.get("event") != "messages.upsert":
            raise IgnoredEvent(f"unhandled event: {payload.get('event')!r}")

        data = payload.get("data") or {}
        key = data.get("key") or {}

        if key.get("fromMe"):
            raise IgnoredEvent("message is fromMe (outbound echo)")

        message = data.get("message") or {}
        text = message.get("conversation") or (message.get("extendedTextMessage") or {}).get(
            "text"
        )

        raw_extra: dict[str, Any] | None = None
        if text:
            msg_type = "text"
            content = text
        elif "imageMessage" in message:
            image = message["imageMessage"] or {}
            msg_type = "image"
            content = image.get("caption") or ""
            raw_extra = {
                "kind": "image",
                "mimetype": image.get("mimetype") or "image/jpeg",
            }
        elif "audioMessage" in message:
            audio = message["audioMessage"] or {}
            msg_type = "audio"
            content = ""
            raw_extra = {
                "kind": "audio",
                "mimetype": audio.get("mimetype") or "audio/ogg",
            }
        elif any(kind in message for kind in _UNSUPPORTED_KINDS):
            kind = next(k for k in _UNSUPPORTED_KINDS if k in message)
            msg_type = "unsupported"
            content = _UNSUPPORTED_KINDS[kind]
        else:
            logger.info("ignoring non-text evolution message, keys=%s", list(message.keys()))
            raise IgnoredEvent("non-text message")

        remote_jid = key.get("remoteJid") or ""
        external_user_id = remote_jid.removesuffix(_JID_SUFFIX)

        raw_ts = data.get("messageTimestamp")
        timestamp = (
            datetime.fromtimestamp(int(raw_ts), tz=UTC)
            if raw_ts is not None
            else datetime.now(UTC)
        )

        raw: dict[str, Any] = dict(payload)
        if raw_extra is not None:
            raw["_media"] = raw_extra

        return IncomingMessage(
            tenant_id=UUID(str(payload["tenant_id"])),
            channel_id=UUID(str(payload["channel_id"])),
            external_user_id=external_user_id,
            external_message_id=str(key["id"]),
            type=msg_type,
            content=content,
            timestamp=timestamp,
            raw=raw,
        )

    async def fetch_media(self, incoming: IncomingMessage, channel: Channel) -> IncomingMedia:
        base_url, instance, apikey = self._connection(channel)

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                f"{base_url}/chat/getBase64FromMediaMessage/{instance}",
                headers={"apikey": apikey},
                json={
                    "message": {"key": {"id": incoming.external_message_id}},
                    "convertToMp4": False,
                },
            )
        response.raise_for_status()
        body = response.json()
        fallback_mime = (incoming.raw.get("_media") or {}).get("mimetype")
        return IncomingMedia(
            data=base64.b64decode(body["base64"]),
            mime=body.get("mimetype") or fallback_mime,
        )

    async def send_message(
        self, message: OutgoingMessage, contact: Contact, channel: Channel
    ) -> None:
        config = channel.config or {}
        base_url = str(config["base_url"]).rstrip("/")
        instance = config["instance"]
        apikey = (
            secrets_crypto.decrypt(channel.secret_encrypted) if channel.secret_encrypted else ""
        )

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{base_url}/message/sendText/{instance}",
                headers={"apikey": apikey},
                json={"number": contact.external_user_id, "text": message.content},
            )
        response.raise_for_status()

    async def mark_read(
        self, external_message_id: str, contact: Contact, channel: Channel
    ) -> None:
        base_url, instance, apikey = self._connection(channel)

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{base_url}/chat/markMessageAsRead/{instance}",
                headers={"apikey": apikey},
                json={
                    "readMessages": [
                        {
                            "remoteJid": f"{contact.external_user_id}{_JID_SUFFIX}",
                            "fromMe": False,
                            "id": external_message_id,
                        }
                    ]
                },
            )
        response.raise_for_status()

    async def send_typing(
        self, contact: Contact, channel: Channel, duration_seconds: float
    ) -> None:
        base_url, instance, apikey = self._connection(channel)

        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                f"{base_url}/chat/sendPresence/{instance}",
                headers={"apikey": apikey},
                json={
                    "number": contact.external_user_id,
                    "presence": "composing",
                    "delay": int(duration_seconds * 1000),
                },
            )
        response.raise_for_status()

    def _connection(self, channel: Channel) -> tuple[str, str, str]:
        config = channel.config or {}
        base_url = str(config["base_url"]).rstrip("/")
        instance = config["instance"]
        apikey = (
            secrets_crypto.decrypt(channel.secret_encrypted) if channel.secret_encrypted else ""
        )
        return base_url, instance, apikey
