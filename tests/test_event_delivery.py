"""Push of external channel events to `events_url` (`app/events/delivery.py`):
signed, in order per channel, retried on a backoff and given up after the
maximum, never to a private address. HTTP is an `httpx.MockTransport`.
"""

import json
import socket
import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.events import delivery
from app.events.delivery import BACKOFF_SECONDS, deliver_due
from app.events.signing import sign, verify
from app.models.channel_event import ChannelEvent
from tests.test_external_channel import _agent, _channel, _post, _run_turn, _tenant

T0 = datetime(2030, 1, 1, tzinfo=UTC)


@pytest.fixture
def factory(test_engine):
    return async_sessionmaker(bind=test_engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def _no_pending_pushes_left(factory):
    """Other tests' worker loops push for real: leave nothing pending behind."""
    yield
    async with factory() as session:
        await session.execute(
            text(
                "UPDATE channel_events SET gave_up_at = now() "
                "WHERE delivered_at IS NULL AND gave_up_at IS NULL AND channel_id IN "
                "(SELECT id FROM channels WHERE config ? 'events_url')"
            )
        )
        await session.commit()


class Inbox:
    """A fake project endpoint: records requests, answers per host."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.status: dict[str, int] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status.get(request.url.host, 200))

    def to(self, url: str) -> list[httpx.Request]:
        return [r for r in self.requests if str(r.url) == url]


@pytest_asyncio.fixture
async def inbox():
    box = Inbox()
    async with httpx.AsyncClient(transport=httpx.MockTransport(box.handler)) as http:
        box.http = http
        yield box


def _url() -> str:
    return f"https://inbox-{uuid.uuid4().hex[:10]}.example.com/events"


async def _external(client, url: str | None):
    """A tenant with an external channel (and one conversation on it)."""
    headers = await _tenant(client)
    agent_id = await _agent(client, headers)
    config = {"events_url": url} if url else {}
    created = await _channel(client, headers, agent_id, config=config)
    assert created.status_code == 201, created.text
    body = created.json()
    posted = await _post(client, headers, body["id"], "oi")
    assert posted.status_code == 201, posted.text
    return headers, body, uuid.UUID(posted.json()["conversation_id"])


async def _add_events(factory, channel: dict, conversation_id, count: int) -> list[uuid.UUID]:
    ids = []
    async with factory() as session:
        tenant_id = (
            await session.execute(
                text("SELECT tenant_id FROM channels WHERE id = :id"), {"id": channel["id"]}
            )
        ).scalar_one()
        for n in range(count):
            event = ChannelEvent(
                id=uuid.uuid4(),
                tenant_id=tenant_id,
                channel_id=uuid.UUID(channel["id"]),
                conversation_id=conversation_id,
                type="message.reply",
                data={"n": n},
            )
            session.add(event)
            await session.flush()
            ids.append(event.id)
        await session.commit()
    return ids


async def _event(factory, event_id) -> ChannelEvent:
    async with factory() as session:
        return await session.scalar(select(ChannelEvent).where(ChannelEvent.id == event_id))


def _sent_ids(requests) -> list[str]:
    return [json.loads(r.content)["id"] for r in requests]


# --- signing ---------------------------------------------------------------------


def test_verify_accepts_its_signature_and_rejects_tampering():
    body = b'{"a":1}'
    signature = sign("s3cret", 1_000, body)
    assert signature.startswith("sha256=")
    assert verify("s3cret", 1_000, body, signature, now=1_100)
    assert verify("s3cret", "1000", body, signature, now=1_100)
    assert not verify("s3cret", 1_000, b'{"a":2}', signature, now=1_100)
    assert not verify("other", 1_000, body, signature, now=1_100)
    assert not verify("s3cret", 1_000, body, signature, now=1_000 + 301)
    assert not verify("s3cret", 1_001, body, signature, now=1_100)
    assert not verify("s3cret", "nope", body, signature)


# --- delivery --------------------------------------------------------------------


async def test_reply_is_pushed_signed_and_verifies(client, db_session, factory, inbox):
    url = _url()
    headers, channel, conversation_id = await _external(client, url)
    reply = await _run_turn(db_session, conversation_id)

    assert await deliver_due(factory, inbox.http) >= 1
    [request] = inbox.to(url)
    assert request.method == "POST"
    assert request.headers["content-type"] == "application/json"
    body = request.content
    payload = json.loads(body)
    assert set(payload) == {
        "id", "seq", "type", "channel_id", "conversation_id", "created_at", "data"
    }
    assert payload["type"] == "message.reply"
    assert payload["channel_id"] == channel["id"]
    assert payload["conversation_id"] == str(conversation_id)
    assert payload["data"]["message_id"] == str(reply.id)
    assert request.headers["X-Parley-Event-Id"] == payload["id"]

    secret = channel["events_secret"]
    timestamp = request.headers["X-Parley-Timestamp"]
    signature = request.headers["X-Parley-Signature"]
    assert verify(secret, timestamp, body, signature)
    changed = body.replace(b"message.reply", b"message.other")
    assert not verify(secret, timestamp, changed, signature)
    assert not verify("wrong-secret", timestamp, body, signature)
    assert not verify(secret, timestamp, body, signature, now=int(timestamp) + 301)

    stored = await _event(factory, uuid.UUID(payload["id"]))
    assert stored.delivered_at is not None and stored.attempts == 0

    # Delivered once: the next pass sends nothing more.
    await deliver_due(factory, inbox.http)
    assert len(inbox.to(url)) == 1
    feed = await client.get(f"/channels/{channel['id']}/events", headers=headers)
    assert [e["id"] for e in feed.json()] == [payload["id"]]


async def test_failing_url_backs_off_then_gives_up(client, factory, inbox, monkeypatch):
    url = _url()
    inbox.status[httpx.URL(url).host] = 500
    headers, channel, conversation_id = await _external(client, url)
    [event_id] = await _add_events(factory, channel, conversation_id, 1)
    logged: list[str] = []
    real_warning = delivery.logger.warning

    def warning(name, **kw):
        logged.append(name)
        return real_warning(name, **kw)

    monkeypatch.setattr(delivery.logger, "warning", warning)
    max_attempts = get_settings().event_push_max_attempts
    assert max_attempts == 7

    now = T0
    for attempt in range(1, max_attempts + 1):
        await deliver_due(factory, inbox.http, now=now)
        assert len(inbox.to(url)) == attempt
        stored = await _event(factory, event_id)
        assert stored.attempts == attempt
        assert stored.last_error == "HTTP 500"
        if attempt < max_attempts:
            wait = timedelta(seconds=BACKOFF_SECONDS[attempt - 1])
            assert stored.next_attempt_at == now + wait
            # Not due a second before the backoff ends.
            await deliver_due(factory, inbox.http, now=now + wait - timedelta(seconds=1))
            assert len(inbox.to(url)) == attempt
            now = now + wait

    stored = await _event(factory, event_id)
    assert stored.gave_up_at == now and stored.delivered_at is None
    assert "event.push_gave_up" in logged
    await deliver_due(factory, inbox.http, now=now + timedelta(days=1))
    assert len(inbox.to(url)) == max_attempts

    feed = await client.get(f"/channels/{channel['id']}/events", headers=headers)
    assert [e["id"] for e in feed.json()] == [str(event_id)]


async def test_backoff_schedule():
    assert BACKOFF_SECONDS == (5, 15, 60, 300, 900, 1800, 3600)
    assert delivery.backoff_seconds(1) == 5
    assert delivery.backoff_seconds(7) == 3600
    assert delivery.backoff_seconds(12) == 3600


async def test_later_event_waits_for_the_earlier_one(client, factory, inbox):
    url = _url()
    host = httpx.URL(url).host
    inbox.status[host] = 503
    _, channel, conversation_id = await _external(client, url)
    first, second = await _add_events(factory, channel, conversation_id, 2)

    await deliver_due(factory, inbox.http, now=T0)
    await deliver_due(factory, inbox.http, now=T0 + timedelta(seconds=1))
    assert _sent_ids(inbox.to(url)) == [str(first)]
    assert (await _event(factory, second)).attempts == 0

    inbox.status[host] = 200
    delivered = await deliver_due(factory, inbox.http, now=T0 + timedelta(seconds=10))
    assert delivered >= 2
    assert _sent_ids(inbox.to(url)) == [str(first), str(first), str(second)]
    assert (await _event(factory, second)).delivered_at is not None


async def test_later_event_is_pushed_after_the_earlier_gives_up(
    client, factory, inbox, monkeypatch
):
    monkeypatch.setenv("EVENT_PUSH_MAX_ATTEMPTS", "1")
    get_settings.cache_clear()
    try:
        url = _url()
        inbox.status[httpx.URL(url).host] = 400
        _, channel, conversation_id = await _external(client, url)
        first, second = await _add_events(factory, channel, conversation_id, 2)

        await deliver_due(factory, inbox.http, now=T0)
        assert _sent_ids(inbox.to(url)) == [str(first)]
        assert (await _event(factory, first)).gave_up_at == T0

        await deliver_due(factory, inbox.http, now=T0)
        assert _sent_ids(inbox.to(url)) == [str(first), str(second)]
    finally:
        get_settings.cache_clear()


async def test_private_address_is_never_called(client, factory, inbox, monkeypatch):
    # Accepted at create (a hostname), but it resolves to a private address.
    url = _url()
    host = httpx.URL(url).host
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(name, port, *args, **kwargs):
        if name == host:
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.7", port or 0))]
        return real_getaddrinfo(name, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    _, channel, conversation_id = await _external(client, url)
    [event_id] = await _add_events(factory, channel, conversation_id, 1)

    await deliver_due(factory, inbox.http, now=T0)
    assert inbox.to(url) == []
    stored = await _event(factory, event_id)
    assert stored.attempts == 1
    assert stored.last_error.startswith("refused:")


async def test_private_literal_set_behind_the_api_is_never_called(client, factory, inbox):
    _, channel, conversation_id = await _external(client, _url())
    async with factory() as session:
        await session.execute(
            text("UPDATE channels SET config = :config WHERE id = :id"),
            {"config": json.dumps({"events_url": "http://169.254.169.254/x"}), "id": channel["id"]},
        )
        await session.commit()
    [event_id] = await _add_events(factory, channel, conversation_id, 1)

    await deliver_due(factory, inbox.http, now=T0)
    assert not any(r.url.host == "169.254.169.254" for r in inbox.requests)
    assert (await _event(factory, event_id)).last_error.startswith("refused:")


async def test_channel_without_events_url_is_skipped(client, factory, inbox):
    _, channel, conversation_id = await _external(client, None)
    [event_id] = await _add_events(factory, channel, conversation_id, 1)

    await deliver_due(factory, inbox.http, now=T0)
    stored = await _event(factory, event_id)
    assert stored.attempts == 0 and stored.delivered_at is None
    assert stored.next_attempt_at is None
    assert not any(json.loads(r.content)["channel_id"] == channel["id"] for r in inbox.requests)


async def test_redirect_is_not_followed(client, factory):
    url = _url()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == url:
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
        return httpx.Response(200)

    _, channel, conversation_id = await _external(client, url)
    [event_id] = await _add_events(factory, channel, conversation_id, 1)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as http:
        await deliver_due(factory, http, now=T0)
    assert "http://169.254.169.254/" not in seen
    assert (await _event(factory, event_id)).last_error == "HTTP 302"


async def test_timeout_counts_as_a_failed_try(client, factory):
    url = _url()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == url:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200)

    _, channel, conversation_id = await _external(client, url)
    [event_id] = await _add_events(factory, channel, conversation_id, 1)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await deliver_due(factory, http, now=T0)
    stored = await _event(factory, event_id)
    assert stored.attempts == 1 and stored.last_error == "timeout"
    assert stored.next_attempt_at == T0 + timedelta(seconds=5)


async def test_claimed_event_is_not_pushed_twice(client, factory, inbox):
    url = _url()
    _, channel, conversation_id = await _external(client, url)
    [event_id] = await _add_events(factory, channel, conversation_id, 1)
    # Another worker claimed it (lease in the future): this pass leaves it alone.
    async with factory() as session:
        stored = await session.get(ChannelEvent, event_id)
        stored.next_attempt_at = T0 + timedelta(seconds=30)
        await session.commit()
    await deliver_due(factory, inbox.http, now=T0)
    assert inbox.to(url) == []


# --- PATCH -----------------------------------------------------------------------


async def test_patch_refuses_secret_of_an_external_channel(client):
    headers, channel, _ = await _external(client, _url())
    response = await client.patch(
        f"/channels/{channel['id']}", json={"secret": "mine"}, headers=headers
    )
    assert response.status_code == 422
    assert "cannot be set" in response.text


async def test_patch_checks_events_url(client):
    headers, channel, _ = await _external(client, _url())
    refused = await client.patch(
        f"/channels/{channel['id']}",
        json={"config": {"events_url": "http://10.0.0.5/events"}},
        headers=headers,
    )
    assert refused.status_code == 422
    assert "invalid events_url" in refused.text
    accepted = await client.patch(
        f"/channels/{channel['id']}", json={"config": {"events_url": _url()}}, headers=headers
    )
    assert accepted.status_code == 200, accepted.text


# --- worker loop -----------------------------------------------------------------


async def test_push_loop_survives_errors_and_stops(monkeypatch):
    import asyncio

    import app.workers.main as worker_main

    stop = asyncio.Event()
    calls = []

    async def failing(session_factory, http, now=None):
        calls.append(1)
        if len(calls) >= 3:
            stop.set()
        raise RuntimeError("database down")

    monkeypatch.setattr(worker_main, "deliver_due", failing)
    await asyncio.wait_for(worker_main.event_push_loop(None, stop, 0.01), 2)
    assert len(calls) == 3
