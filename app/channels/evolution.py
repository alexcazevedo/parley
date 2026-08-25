"""WhatsApp channel via Evolution API: webhook ingestion (`messages.upsert`)
and outbound delivery via Evolution's `sendText` REST endpoint.

Channel `config`: `{base_url, instance}`. `secret_encrypted` holds the
Evolution `apikey`, used both to authenticate inbound webhooks (`app/api/
webhooks.py` compares it against the `apikey` header) and as the outbound
`apikey` header on `send_message`.
"""

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx

from app.channels.base import ChannelAdapter, IncomingMessage, OutgoingMessage
from app.models.channel import Channel
from app.models.contact import Contact
from app.security import secrets as secrets_crypto

logger = logging.getLogger(__name__)

_JID_SUFFIX = "@s.whatsapp.net"


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
        if not text:
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

        return IncomingMessage(
            tenant_id=UUID(str(payload["tenant_id"])),
            channel_id=UUID(str(payload["channel_id"])),
            external_user_id=external_user_id,
            external_message_id=str(key["id"]),
            type="text",
            content=text,
            timestamp=timestamp,
            raw=payload,
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
