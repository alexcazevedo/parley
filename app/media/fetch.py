"""Worker-side download of WhatsApp (Evolution) media, right before the turn.

The webhook stores an image or audio message with its type and `media_key`
null and answers at once (PRODUCT.md E4.4); the bytes are fetched here, by
the worker, at the start of every turn whatever the conversation's state
(an operator handling the chat sees the photo too), before
`prepare_media_messages` transcribes them and the turn joins the batch.
The download goes through `EvolutionAdapter.fetch_media`, so the channel's
connection settings and SSRF check apply exactly as on every other outbound
call, and the file is stored under the message's own tenant.

A download that fails or whose media is rejected (`validate_media`, the
same rules as `ingest`) turns the row into `unsupported` /
"[mídia não recebida]", as the webhook did before: the customer gets the
unsupported-media reply. It is not retried: the outcome is committed
before the turn goes on. Rows that already have a
`media_key` (webchat uploads, a retried turn) are left alone.
"""

from collections.abc import Awaitable, Callable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.evolution import EvolutionAdapter
from app.db import end_transaction
from app.media.storage import get_media_storage
from app.messages.ingest import validate_media
from app.models.channel import Channel, ChannelType
from app.models.message import Message

logger = structlog.get_logger(__name__)

MEDIA_NOT_RECEIVED_TEXT = "[mídia não recebida]"
_DOWNLOADED_TYPES = ("image", "audio")


async def fetch_pending_media(
    session: AsyncSession,
    messages: list[Message],
    channel: Channel,
    *,
    ownership_check: Callable[[], Awaitable[bool]] | None = None,
) -> bool:
    """Download and store the media of every pending image/audio row, in place.

    Only on a `whatsapp_evolution` channel. The transaction is ended
    (`app.db.end_transaction`) right before each download, and never when
    nothing is pending. Logs `media.fetch_failed` with the message id only:
    no payload, base64 or JID. A refused channel endpoint fails every pending
    row (the adapter's SSRF check raises before any request).

    The result is committed at the end (`end_transaction`): a rollback later
    in the turn keeps the stored `media_key` (no second download on retry)
    and keeps a failed download failed.

    `ownership_check` ("is the conversation lock still ours?") is asked right
    before each download; a `False`, or a check that raises, stops: nothing
    more is downloaded and `False` is returned, after committing the rows
    already handled. Otherwise returns `True`.
    """
    if channel.type != ChannelType.WHATSAPP_EVOLUTION:
        return True
    pending = [
        message
        for message in messages
        if message.type in _DOWNLOADED_TYPES and not message.media_key
    ]
    if not pending:
        return True

    adapter = EvolutionAdapter()
    storage = get_media_storage()
    owned = True
    for message in pending:
        if not await _still_owned(ownership_check):
            owned = False
            break
        # Read before `end_transaction`: nothing below needs a lazy load.
        message_id = message.id
        tenant_id = message.tenant_id
        message_type = message.type
        external_message_id = message.external_message_id
        fallback_mime = ((message.raw_payload or {}).get("_media") or {}).get("mimetype")
        try:
            if not external_message_id:
                raise ValueError("message has no external id")
            await end_transaction(session)
            media = await adapter.fetch_media(external_message_id, fallback_mime, channel)
            mime = validate_media(message_type, media)
            message.media_key = await storage.put(tenant_id, media.data, mime)
            message.media_mime = mime
        except Exception:
            message.type = "unsupported"
            message.content = MEDIA_NOT_RECEIVED_TEXT
            message.media_key = None
            message.media_mime = None
            logger.warning("media.fetch_failed", message_id=str(message_id))

    await end_transaction(session)
    return owned


async def _still_owned(check: Callable[[], Awaitable[bool]] | None) -> bool:
    """No check: owned. A check that raises counts as not owned (as in the turn)."""
    if check is None:
        return True
    try:
        return bool(await check())
    except Exception:
        logger.warning("media.ownership_check_failed", exc_info=True)
        return False
