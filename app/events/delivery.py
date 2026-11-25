"""Push of `external` channel events to the channel's `config.events_url`.

`deliver_due` is one pass of the worker's push loop (`app/workers/main.py`).
For each `external` channel with an `events_url` it takes the channel's head
event (lowest `seq` with neither `delivered_at` nor `gave_up_at`) and, when it
is due (`next_attempt_at` null or past), POSTs it. A later event of a channel
is never pushed while an earlier one is pending: only the head is considered,
and after a failure the channel waits for the head's next try.

Claim: the head is re-read `FOR UPDATE SKIP LOCKED` with its due condition,
then `next_attempt_at` is moved one lease ahead and the transaction commits.
Another worker then sees the head as not due and skips the channel; no lock or
transaction is held across the HTTP call. A worker that dies mid-push leaves
the lease, and the event is tried again after it (without counting a try).

Outcome: a 2xx sets `delivered_at`. Anything else (other status, timeout,
connection error, SSRF refusal) adds one to `attempts`, stores `last_error`
and schedules the next try on `BACKOFF_SECONDS`; the `EVENT_PUSH_MAX_ATTEMPTS`th
failure sets `gave_up_at` and logs `event.push_gave_up`. A given-up event
stays in the feed (`GET /channels/{channel_id}/events`), which is the
recovery path, and no longer blocks the channel.

The URL is tenant-controlled, so every push runs `assert_safe_channel_url`
(DNS resolved, every address public, `CHANNEL_ALLOWED_HOSTS` honoured) and
never follows redirects. Same accepted TOCTOU limitation as the Evolution
channel (`app/tools/ssrf.py`): the address is not pinned into the connection.

Neither the secret nor the signature is ever logged.
"""

import asyncio
import json
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.events.signing import sign
from app.models.channel import Channel, ChannelType
from app.models.channel_event import ChannelEvent
from app.security import secrets as secrets_crypto
from app.tools.ssrf import SSRFError, assert_safe_channel_url

logger = structlog.get_logger(__name__)

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

HEADER_EVENT_ID = "X-Parley-Event-Id"
HEADER_TIMESTAMP = "X-Parley-Timestamp"
HEADER_SIGNATURE = "X-Parley-Signature"

# Wait before try n+1 after the n-th failure; past the end the last one repeats.
BACKOFF_SECONDS = (5, 15, 60, 300, 900, 1800, 3600)
# How far a claim moves `next_attempt_at` ahead, beyond the push timeout.
CLAIM_LEASE_MARGIN_SECONDS = 30
# Events one channel may push in one pass while each succeeds.
MAX_EVENTS_PER_CHANNEL_PASS = 20
# Channels pushed at once in one pass.
CHANNEL_CONCURRENCY = 8
_MAX_ERROR_CHARS = 500


@dataclass(frozen=True)
class _Target:
    channel_id: uuid.UUID
    tenant_id: uuid.UUID
    url: str
    secret_encrypted: str | None


def backoff_seconds(attempts: int) -> int:
    """Wait after the `attempts`-th failed try (1-based)."""
    return BACKOFF_SECONDS[min(max(attempts, 1), len(BACKOFF_SECONDS)) - 1]


def event_body(event: ChannelEvent) -> bytes:
    """The exact bytes POSTed (and signed) for `event`."""
    payload: dict[str, Any] = {
        "id": str(event.id),
        "seq": event.seq,
        "type": event.type,
        "channel_id": str(event.channel_id),
        "conversation_id": str(event.conversation_id),
        "created_at": event.created_at.isoformat(),
        "data": event.data,
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


async def _targets(session_factory: SessionFactory) -> list[_Target]:
    async with session_factory() as session:
        rows = await session.execute(
            select(Channel.id, Channel.tenant_id, Channel.config, Channel.secret_encrypted).where(
                Channel.type == ChannelType.EXTERNAL,
                Channel.config["events_url"].astext.is_not(None),
            )
        )
        targets = [
            _Target(row.id, row.tenant_id, str(row.config["events_url"]), row.secret_encrypted)
            for row in rows
            if row.config.get("events_url")
        ]
        await session.rollback()
    return targets


def _pending(target: _Target) -> list[Any]:
    return [
        ChannelEvent.tenant_id == target.tenant_id,
        ChannelEvent.channel_id == target.channel_id,
        ChannelEvent.delivered_at.is_(None),
        ChannelEvent.gave_up_at.is_(None),
    ]


async def _claim(
    session_factory: SessionFactory, target: _Target, now: datetime, lease: timedelta
) -> ChannelEvent | None:
    """The channel's head event when it is due and free, claimed; else `None`."""
    async with session_factory() as session:
        head_id = await session.scalar(
            select(ChannelEvent.id)
            .where(*_pending(target))
            .order_by(ChannelEvent.seq)
            .limit(1)
        )
        if head_id is None:
            await session.rollback()
            return None
        # Re-read under a row lock: another worker holding it, or having
        # delivered or claimed it meanwhile, means this channel is skipped.
        event = await session.scalar(
            select(ChannelEvent)
            .where(
                ChannelEvent.id == head_id,
                *_pending(target),
                (ChannelEvent.next_attempt_at.is_(None)) | (ChannelEvent.next_attempt_at <= now),
            )
            .with_for_update(skip_locked=True)
        )
        if event is None:
            await session.rollback()
            return None
        event.next_attempt_at = now + lease
        await session.commit()
        return event


async def _push(http: httpx.AsyncClient, target: _Target, event: ChannelEvent) -> str | None:
    """POST `event`; `None` on a 2xx, else the reason it failed."""
    if not target.secret_encrypted:
        return "channel has no secret"
    try:
        secret = secrets_crypto.decrypt(target.secret_encrypted)
    except Exception:
        return "channel secret cannot be read"
    try:
        await assert_safe_channel_url(target.url)
    except SSRFError as exc:
        logger.warning(
            "event.push_refused",
            channel_id=str(target.channel_id),
            tenant_id=str(target.tenant_id),
            reason=str(exc),
        )
        return f"refused: {exc}"

    body = event_body(event)
    timestamp = int(datetime.now(UTC).timestamp())
    headers = {
        "Content-Type": "application/json",
        HEADER_EVENT_ID: str(event.id),
        HEADER_TIMESTAMP: str(timestamp),
        HEADER_SIGNATURE: sign(secret, timestamp, body),
    }
    timeout = get_settings().event_push_timeout_seconds
    try:
        # The overall deadline also bounds a server that drips its headers.
        async with asyncio.timeout(timeout):
            # Streamed and never read: the receiver's body is not needed.
            async with http.stream(
                "POST",
                target.url,
                content=body,
                headers=headers,
                timeout=timeout,
                follow_redirects=False,
            ) as response:
                status = response.status_code
    except (TimeoutError, httpx.TimeoutException):
        return "timeout"
    except httpx.HTTPError as exc:
        return f"error: {type(exc).__name__}"
    if 200 <= status < 300:
        return None
    return f"HTTP {status}"


async def _record(
    session_factory: SessionFactory,
    target: _Target,
    event: ChannelEvent,
    error: str | None,
    now: datetime,
) -> None:
    values: dict[str, Any]
    if error is None:
        values = {"delivered_at": now, "next_attempt_at": None, "last_error": None}
    else:
        attempts = event.attempts + 1
        values = {"attempts": attempts, "last_error": error[:_MAX_ERROR_CHARS]}
        gave_up = attempts >= get_settings().event_push_max_attempts
        if gave_up:
            values |= {"gave_up_at": now, "next_attempt_at": None}
        else:
            values["next_attempt_at"] = now + timedelta(seconds=backoff_seconds(attempts))
        log = logger.warning if gave_up else logger.info
        log(
            "event.push_gave_up" if gave_up else "event.push_failed",
            event_id=str(event.id),
            seq=event.seq,
            channel_id=str(target.channel_id),
            tenant_id=str(target.tenant_id),
            attempts=attempts,
            error=values["last_error"],
        )
    async with session_factory() as session:
        await session.execute(
            update(ChannelEvent)
            .where(ChannelEvent.id == event.id, ChannelEvent.tenant_id == target.tenant_id)
            .values(**values)
        )
        await session.commit()


async def _deliver_channel(
    session_factory: SessionFactory,
    http: httpx.AsyncClient,
    target: _Target,
    now: datetime,
) -> int:
    lease = timedelta(
        seconds=get_settings().event_push_timeout_seconds + CLAIM_LEASE_MARGIN_SECONDS
    )
    delivered = 0
    for _ in range(MAX_EVENTS_PER_CHANNEL_PASS):
        event = await _claim(session_factory, target, now, lease)
        if event is None:
            break
        error = await _push(http, target, event)
        await _record(session_factory, target, event, error, now)
        if error is not None:
            break
        delivered += 1
    return delivered


async def deliver_due(
    session_factory: SessionFactory,
    http: httpx.AsyncClient,
    now: datetime | None = None,
) -> int:
    """One push pass over every `external` channel with an `events_url`.

    Returns how many events were delivered (2xx). `now` (default: the clock)
    decides what is due and stamps `delivered_at`, `gave_up_at` and
    `next_attempt_at`. An error in one channel is logged and does not stop
    the others.
    """
    moment = now if now is not None else datetime.now(UTC)
    targets = await _targets(session_factory)
    if not targets:
        return 0
    gate = asyncio.Semaphore(CHANNEL_CONCURRENCY)

    async def one(target: _Target) -> int:
        async with gate:
            try:
                return await _deliver_channel(session_factory, http, target, moment)
            except Exception:
                logger.exception(
                    "event.push_channel_error",
                    channel_id=str(target.channel_id),
                    tenant_id=str(target.tenant_id),
                )
                return 0

    return sum(await asyncio.gather(*(one(t) for t in targets)))
