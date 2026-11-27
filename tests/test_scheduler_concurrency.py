"""Conversations run concurrently (PRODUCT.md E3.4).

One worker runs up to `worker_concurrency` turns at once. `tick` runs the due
conversations side by side and waits for all of them; `run_scheduler` keeps a
pool of running turns across loops and claims a conversation only when a slot
is free. On `stop` it claims nothing new and waits for the running turns.

The runtime really sleeps (fractions of a second) and records when each turn
starts and ends, so overlap is measured, not assumed. Every turn gets its own
session from the test engine: one `AsyncSession` cannot serve two turns at
once.
"""

import asyncio
import os
import signal
import time
import uuid
from contextlib import asynccontextmanager

import pytest
import structlog
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings, get_settings
from app.conversations import debounce, lock
from app.conversations import turn as turn_module
from app.conversations.debounce import DUE_KEY, inflight_key, pending_key
from app.conversations.retry import RETRY_BACKOFF_SECONDS
from app.models.message import Message, MessageRole
from app.queue.redis_queue import JOBS_KEY, RedisQueue
from app.workers import main as worker_main
from app.workers.main import run_once
from app.workers.scheduler import run_scheduler, tick

DEBOUNCE_SECONDS = 0.2
TURN_TASK_PREFIX = "scheduler-turn:"
HEARTBEAT_PREFIX = "lock-heartbeat:"
FAR_FUTURE = 10_000.0


class Boom(Exception):
    pass


class Crash(Exception):
    """A Redis bookkeeping step blowing up (not a turn failure)."""


class TimedRuntime:
    """Sleeps per input text, records [start, end] and the peak overlap."""

    def __init__(
        self,
        delays: dict[str, float] | None = None,
        *,
        default: float = 0.2,
        fail: set[str] | None = None,
    ) -> None:
        self.delays = delays or {}
        self.default = default
        self.fail = fail or set()
        self.spans: dict[str, list[float | None]] = {}
        self.calls: list[str] = []
        self.running = 0
        self.max_running = 0
        self._started: dict[str, asyncio.Event] = {}

    def started(self, text: str) -> asyncio.Event:
        return self._started.setdefault(text, asyncio.Event())

    def ended(self, text: str) -> bool:
        return text in self.spans and self.spans[text][1] is not None

    async def run(self, conversation, input_text: str) -> str:
        self.calls.append(input_text)
        self.spans[input_text] = [time.monotonic(), None]
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        self.started(input_text).set()
        try:
            await asyncio.sleep(self.delays.get(input_text, self.default))
            if input_text in self.fail:
                raise Boom(f"turn for {input_text!r} exploded")
            return f"re: {input_text}"
        finally:
            self.running -= 1
            self.spans[input_text][1] = time.monotonic()


class RecordingAdapter:
    def __init__(self) -> None:
        self.sent: list[tuple[uuid.UUID, str]] = []

    def parse_incoming(self, payload):  # pragma: no cover - unused here
        raise NotImplementedError

    async def mark_read(self, external_message_id, contact, channel) -> None:
        return None

    async def send_typing(self, contact, channel, duration_seconds) -> None:
        return None

    async def send_message(self, message, contact, channel) -> None:
        self.sent.append((message.conversation_id, message.content))


@pytest.fixture
def adapter(monkeypatch) -> RecordingAdapter:
    recording = RecordingAdapter()
    monkeypatch.setattr(turn_module, "get_adapter", lambda channel_type: recording)
    return recording


@pytest.fixture
def sessions(test_engine):
    maker = async_sessionmaker(bind=test_engine, expire_on_commit=False)

    @asynccontextmanager
    async def factory():
        async with maker() as session:
            yield session

    return factory


async def _conversation(client, fake_redis, text: str) -> uuid.UUID:
    tenant = await client.post(
        "/tenants", json={"name": "Acme"}, headers={"X-Admin-Key": os.environ["ADMIN_API_KEY"]}
    )
    headers = {"X-API-Key": tenant.json()["api_key"]}
    agent = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Hi.", "model": "gpt-4o-mini"},
        headers=headers,
    )
    assert agent.status_code == 201, agent.text
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    assert channel.status_code == 201, channel.text
    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel.json()["id"], "user_id": "user-1", "message": text},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    queue = RedisQueue(fake_redis)
    while await fake_redis.llen(JOBS_KEY) > 0:
        assert await run_once(queue, timeout=1, debounce_seconds=DEBOUNCE_SECONDS) is not None
    return uuid.UUID(response.json()["conversation_id"])


async def _replies(sessions, conversation_id: uuid.UUID) -> list[str]:
    async with sessions() as session:
        result = await session.execute(
            select(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.role == MessageRole.ASSISTANT,
            )
            .order_by(Message.seq)
        )
        return [row.content for row in result.scalars().all()]


def _overlap(a: list, b: list) -> bool:
    return a[0] < b[1] and b[0] < a[1]


def _live_tasks(prefix: str) -> list[asyncio.Task]:
    return [
        task
        for task in asyncio.all_tasks()
        if task.get_name().startswith(prefix) and not task.done()
    ]


async def _until(predicate, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(0.01)


async def _stop_and_reap(task: asyncio.Task, stop: asyncio.Event) -> None:
    stop.set()
    if not task.done():
        try:
            await asyncio.wait_for(asyncio.shield(task), 3)
        except TimeoutError:
            task.cancel()
    await asyncio.gather(task, return_exceptions=True)


# --- settings ------------------------------------------------------------------


def test_worker_concurrency_setting(monkeypatch):
    monkeypatch.delenv("WORKER_CONCURRENCY", raising=False)
    assert Settings().worker_concurrency == 4
    monkeypatch.setenv("WORKER_CONCURRENCY", "7")
    assert Settings().worker_concurrency == 7


@pytest.mark.parametrize("value", ["0", "-1"])
def test_worker_concurrency_below_one_is_rejected(monkeypatch, value):
    monkeypatch.setenv("WORKER_CONCURRENCY", value)
    with pytest.raises(ValidationError):
        Settings()


async def test_tick_rejects_concurrency_below_one(fake_redis, sessions):
    with pytest.raises(ValueError):
        await tick(fake_redis, sessions, concurrency=0)


# --- `tick` ----------------------------------------------------------------------


async def test_two_due_conversations_overlap_by_default(
    client, fake_redis, sessions, adapter
):
    first = await _conversation(client, fake_redis, "primeira")
    second = await _conversation(client, fake_redis, "segunda")
    runtime = TimedRuntime(default=0.3)

    processed = await tick(fake_redis, sessions, runtime, now=time.time() + 5)

    assert processed == [first, second]  # due order, as before
    assert _overlap(runtime.spans["primeira"], runtime.spans["segunda"])
    assert get_settings().worker_concurrency == 4
    assert await _replies(sessions, first) == ["re: primeira"]
    assert await _replies(sessions, second) == ["re: segunda"]
    assert _live_tasks(HEARTBEAT_PREFIX) == []


async def test_two_due_conversations_overlap_with_concurrency_two(
    client, fake_redis, sessions, adapter
):
    first = await _conversation(client, fake_redis, "primeira")
    second = await _conversation(client, fake_redis, "segunda")
    runtime = TimedRuntime(default=0.3)

    started = time.monotonic()
    processed = await tick(fake_redis, sessions, runtime, now=time.time() + 5, concurrency=2)
    elapsed = time.monotonic() - started

    assert processed == [first, second]
    assert _overlap(runtime.spans["primeira"], runtime.spans["segunda"])
    assert elapsed < 0.55  # two 0.3 s turns, side by side
    assert sorted(adapter.sent) == sorted([(first, "re: primeira"), (second, "re: segunda")])
    assert await _replies(sessions, first) == ["re: primeira"]
    assert await _replies(sessions, second) == ["re: segunda"]


async def test_concurrency_one_runs_one_after_the_other(
    client, fake_redis, sessions, adapter
):
    first = await _conversation(client, fake_redis, "primeira")
    second = await _conversation(client, fake_redis, "segunda")
    runtime = TimedRuntime(default=0.1)

    processed = await tick(fake_redis, sessions, runtime, now=time.time() + 5, concurrency=1)

    assert processed == [first, second]
    assert runtime.calls == ["primeira", "segunda"]
    assert runtime.spans["primeira"][1] <= runtime.spans["segunda"][0]
    assert runtime.max_running == 1


async def test_three_due_with_concurrency_two_never_exceed_two(
    client, fake_redis, sessions, adapter
):
    ids = [await _conversation(client, fake_redis, text) for text in ("um", "dois", "tres")]
    runtime = TimedRuntime(default=0.2)

    processed = await tick(fake_redis, sessions, runtime, now=time.time() + 5, concurrency=2)

    assert processed == ids
    assert runtime.max_running == 2
    for cid, text in zip(ids, ("um", "dois", "tres"), strict=True):
        assert await _replies(sessions, cid) == [f"re: {text}"]
    assert await fake_redis.zcard(DUE_KEY) == 0


async def test_failing_turn_does_not_stop_a_healthy_one(
    client, fake_redis, sessions, adapter
):
    failing = await _conversation(client, fake_redis, "falha")
    healthy = await _conversation(client, fake_redis, "tudo bem")
    runtime = TimedRuntime(default=0.2, fail={"falha"})
    now = time.time() + 5

    processed = await tick(fake_redis, sessions, runtime, now=now, concurrency=2)

    assert processed == [healthy]
    assert _overlap(runtime.spans["falha"], runtime.spans["tudo bem"])
    assert await _replies(sessions, healthy) == ["re: tudo bem"]
    assert await _replies(sessions, failing) == []
    # The failed one is handled as before: back in pending, due after the backoff.
    assert await fake_redis.zscore(DUE_KEY, str(failing)) == now + RETRY_BACKOFF_SECONDS[0]
    assert await fake_redis.llen(pending_key(failing)) == 1
    assert await fake_redis.zscore(DUE_KEY, str(healthy)) is None


async def test_bookkeeping_crash_in_one_does_not_cancel_or_hide_the_others(
    client, fake_redis, sessions, adapter, monkeypatch
):
    crashing = await _conversation(client, fake_redis, "quebra")
    healthy = await _conversation(client, fake_redis, "tudo bem")
    now = time.time() + 5
    await debounce.schedule(fake_redis, crashing, now - 10)  # first in the tick
    await debounce.schedule(fake_redis, healthy, now - 5)
    runtime = TimedRuntime(default=0.2)
    original_acquire = lock.acquire

    async def acquire(redis, conversation_id, *args, **kwargs):
        if str(conversation_id) == str(crashing):
            await asyncio.sleep(0.05)
            raise Crash("redis went away")
        return await original_acquire(redis, conversation_id, *args, **kwargs)

    monkeypatch.setattr(lock, "acquire", acquire)

    with structlog.testing.capture_logs() as logs, pytest.raises(Crash):
        await tick(fake_redis, sessions, runtime, now=now, concurrency=2)

    # The healthy turn ran to the end, reply stored, nothing left behind.
    assert runtime.calls == ["tudo bem"]
    assert await _replies(sessions, healthy) == ["re: tudo bem"]
    assert await fake_redis.zscore(DUE_KEY, str(healthy)) is None
    assert await fake_redis.exists(lock.lock_key(healthy)) == 0
    # The crashed one stays claimed: the claim timeout brings it back.
    assert await fake_redis.zscore(DUE_KEY, str(crashing)) == (
        now + debounce.CLAIM_TIMEOUT_SECONDS
    )
    crashed = [entry for entry in logs if entry["event"] == "scheduler.turn_crashed"]
    assert [entry["conversation_id"] for entry in crashed] == [str(crashing)]
    assert crashed[0]["log_level"] == "error"
    assert _live_tasks(HEARTBEAT_PREFIX) == []


async def test_same_conversation_never_runs_twice_in_parallel(
    client, fake_redis, sessions, adapter
):
    cid = await _conversation(client, fake_redis, "oi")
    runtime = TimedRuntime(default=0.2)
    now = time.time() + 5

    results = await asyncio.gather(
        tick(fake_redis, sessions, runtime, now=now, concurrency=4),
        tick(fake_redis, sessions, runtime, now=now, concurrency=4),
    )

    assert sorted(results, key=len) == [[], [cid]]
    assert runtime.calls == ["oi"]
    assert await _replies(sessions, cid) == ["re: oi"]


# --- `run_scheduler` ---------------------------------------------------------------


async def test_run_scheduler_starts_newly_due_turn_while_slow_one_runs(
    client, fake_redis, sessions, adapter
):
    slow = await _conversation(client, fake_redis, "devagar")
    quick = await _conversation(client, fake_redis, "rapido")
    await debounce.schedule(fake_redis, slow, time.time())
    await debounce.schedule(fake_redis, quick, time.time() + FAR_FUTURE)
    runtime = TimedRuntime({"devagar": 0.8, "rapido": 0.05})
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_scheduler(fake_redis, sessions, runtime, interval=0.02, concurrency=2, stop=stop)
    )
    try:
        await asyncio.wait_for(runtime.started("devagar").wait(), 3)
        await debounce.schedule(fake_redis, quick, time.time())

        await asyncio.wait_for(runtime.started("rapido").wait(), 3)
        assert not runtime.ended("devagar")
        await _until(lambda: (quick, "re: rapido") in adapter.sent)
        assert not runtime.ended("devagar")
        # The reply row is stored after its send, so wait for it instead of
        # reading the database the instant the adapter saw the message.
        deadline = time.monotonic() + 3.0
        while await _replies(sessions, quick) != ["re: rapido"]:
            assert time.monotonic() < deadline, "reply not stored in time"
            await asyncio.sleep(0.01)

        await _until(lambda: (slow, "re: devagar") in adapter.sent)
    finally:
        await _stop_and_reap(task, stop)

    assert task.exception() is None
    assert await _replies(sessions, slow) == ["re: devagar"]
    assert await fake_redis.zcard(DUE_KEY) == 0
    assert _live_tasks(TURN_TASK_PREFIX) == []
    assert _live_tasks(HEARTBEAT_PREFIX) == []


async def test_run_scheduler_claims_only_when_a_slot_is_free(
    client, fake_redis, sessions, adapter
):
    slow = await _conversation(client, fake_redis, "devagar")
    waiting = await _conversation(client, fake_redis, "espera")
    await debounce.schedule(fake_redis, slow, time.time())
    await debounce.schedule(fake_redis, waiting, time.time() + FAR_FUTURE)
    runtime = TimedRuntime({"devagar": 0.4, "espera": 0.05})
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_scheduler(fake_redis, sessions, runtime, interval=0.02, concurrency=1, stop=stop)
    )
    try:
        await asyncio.wait_for(runtime.started("devagar").wait(), 3)
        due_at = time.time() - 1
        await debounce.schedule(fake_redis, waiting, due_at)
        await asyncio.sleep(0.15)  # several scheduler loops

        # No free slot: not started and not claimed (its due score is untouched).
        assert "espera" not in runtime.calls
        assert await fake_redis.zscore(DUE_KEY, str(waiting)) == due_at

        await asyncio.wait_for(runtime.started("espera").wait(), 3)
        assert runtime.ended("devagar")
        await _until(lambda: (waiting, "re: espera") in adapter.sent)
    finally:
        await _stop_and_reap(task, stop)

    assert runtime.max_running == 1
    assert await _replies(sessions, waiting) == ["re: espera"]


async def test_stop_finishes_running_turn_and_starts_nothing_new(
    client, fake_redis, sessions, adapter
):
    slow = await _conversation(client, fake_redis, "devagar")
    later = await _conversation(client, fake_redis, "depois")
    await debounce.schedule(fake_redis, slow, time.time())
    await debounce.schedule(fake_redis, later, time.time() + FAR_FUTURE)
    runtime = TimedRuntime({"devagar": 0.4, "depois": 0.05})
    stop = asyncio.Event()
    task = asyncio.create_task(
        run_scheduler(fake_redis, sessions, runtime, interval=0.02, concurrency=2, stop=stop)
    )
    try:
        await asyncio.wait_for(runtime.started("devagar").wait(), 3)
        stop.set()
        due_at = time.time() - 1
        await debounce.schedule(fake_redis, later, due_at)  # due, with a free slot

        await asyncio.sleep(0.1)
        assert not task.done()  # still waiting for the running turn
        await asyncio.wait_for(asyncio.shield(task), 3)
    finally:
        await _stop_and_reap(task, stop)

    assert task.exception() is None
    assert runtime.ended("devagar")
    assert await _replies(sessions, slow) == ["re: devagar"]
    assert (slow, "re: devagar") in adapter.sent
    assert await fake_redis.zscore(DUE_KEY, str(slow)) is None
    assert await fake_redis.exists(lock.lock_key(slow)) == 0
    # Nothing new: the later one was never claimed and still waits in pending.
    assert runtime.calls == ["devagar"]
    assert await fake_redis.zscore(DUE_KEY, str(later)) == due_at
    assert await fake_redis.llen(pending_key(later)) == 1
    assert _live_tasks(TURN_TASK_PREFIX) == []
    assert _live_tasks(HEARTBEAT_PREFIX) == []


async def test_stop_with_nothing_running_returns_at_once(fake_redis, sessions):
    stop = asyncio.Event()
    stop.set()
    await asyncio.wait_for(
        run_scheduler(fake_redis, sessions, TimedRuntime(), interval=0.02, stop=stop), 1
    )


async def test_cancelled_scheduler_cancels_and_awaits_its_turns(
    client, fake_redis, sessions, adapter
):
    cid = await _conversation(client, fake_redis, "devagar")
    await debounce.schedule(fake_redis, cid, time.time())
    runtime = TimedRuntime({"devagar": 5.0})
    task = asyncio.create_task(
        run_scheduler(fake_redis, sessions, runtime, interval=0.02, concurrency=2)
    )
    await asyncio.wait_for(runtime.started("devagar").wait(), 3)

    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert task.cancelled()
    assert _live_tasks(TURN_TASK_PREFIX) == []
    assert _live_tasks(HEARTBEAT_PREFIX) == []
    assert adapter.sent == []
    assert await _replies(sessions, cid) == []
    # Lock released, batch still in flight under its claim: it comes back later.
    assert await fake_redis.exists(lock.lock_key(cid)) == 0
    assert await fake_redis.llen(inflight_key(cid)) == 1
    assert await fake_redis.zscore(DUE_KEY, str(cid)) > time.time() + 30


async def test_run_scheduler_survives_a_crashing_turn(
    client, fake_redis, sessions, adapter, monkeypatch
):
    crashing = await _conversation(client, fake_redis, "quebra")
    healthy = await _conversation(client, fake_redis, "tudo bem")
    await debounce.schedule(fake_redis, crashing, time.time() - 1)
    await debounce.schedule(fake_redis, healthy, time.time() + FAR_FUTURE)
    original_drain = debounce.drain_pending

    async def drain(redis, conversation_id):
        if str(conversation_id) == str(crashing):
            raise Crash("redis went away")
        return await original_drain(redis, conversation_id)

    monkeypatch.setattr(debounce, "drain_pending", drain)
    runtime = TimedRuntime(default=0.05)
    stop = asyncio.Event()
    with structlog.testing.capture_logs() as logs:
        task = asyncio.create_task(
            run_scheduler(fake_redis, sessions, runtime, interval=0.02, concurrency=2, stop=stop)
        )
        try:
            await _until(lambda: any(e["event"] == "scheduler.turn_crashed" for e in logs))
            await debounce.schedule(fake_redis, healthy, time.time())
            await _until(lambda: (healthy, "re: tudo bem") in adapter.sent)
        finally:
            await _stop_and_reap(task, stop)

    assert task.exception() is None
    assert await fake_redis.exists(lock.lock_key(crashing)) == 0


# --- `main` ------------------------------------------------------------------------


class ClosableRedis:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def worker(monkeypatch):
    """`main` with its Redis, runtime and loops replaced by recorders."""
    state: dict = {"run_once": 0, "redis": ClosableRedis()}
    monkeypatch.setattr(worker_main.Redis, "from_url", lambda *a, **kw: state["redis"])
    monkeypatch.setattr(worker_main, "AgentRuntime", lambda **kwargs: object())

    async def fake_run_once(queue, *args, **kwargs):
        state["run_once"] += 1
        await asyncio.sleep(0.01)
        return None

    async def fake_housekeeping(queue, *args, **kwargs):
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["housekeeping_cancelled"] = True
            raise

    async def idle_session_end(session_factory, stop, interval) -> None:
        # The real loop would take `pending` sessions other tests left in the
        # shared database and call the model for their recaps. This test is
        # about stopping the worker, so the loop only waits for the stop.
        await stop.wait()

    monkeypatch.setattr(worker_main, "run_once", fake_run_once)
    monkeypatch.setattr(worker_main, "housekeeping_loop", fake_housekeeping)
    monkeypatch.setattr(worker_main, "session_end_loop", idle_session_end)
    return state


def _replaced(sig: signal.Signals, before) -> bool:
    """`main` put its own handler in place (so raising `sig` cannot kill pytest)."""
    return signal.getsignal(sig) is not before


@asynccontextmanager
async def _running_main():
    task = asyncio.create_task(worker_main.main())
    try:
        yield task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_signal_stops_worker_after_running_turns(worker, monkeypatch, sig):
    before = signal.getsignal(sig)

    async def fake_scheduler(redis, session_factory, runtime=None, *args, **kwargs):
        worker["concurrency"] = kwargs.get("concurrency")
        await kwargs["stop"].wait()
        await asyncio.sleep(0.1)  # the running turns finishing
        worker["scheduler_finished"] = True

    monkeypatch.setattr(worker_main, "run_scheduler", fake_scheduler)
    async with _running_main() as main_task:
        await _until(lambda: worker["run_once"] > 0 and _replaced(sig, before))
        os.kill(os.getpid(), sig)
        await asyncio.wait_for(asyncio.shield(main_task), 3)

    assert worker["scheduler_finished"] is True
    assert worker["concurrency"] == get_settings().worker_concurrency
    assert worker["housekeeping_cancelled"] is True
    assert worker["redis"].closed is True
    calls = worker["run_once"]
    await asyncio.sleep(0.05)
    assert worker["run_once"] == calls  # the job loop ended
    assert signal.getsignal(sig) == before  # handler restored


async def test_shutdown_cancels_scheduler_after_grace(worker, monkeypatch):
    monkeypatch.setattr(worker_main, "SHUTDOWN_GRACE_SECONDS", 0.1)

    async def stuck_scheduler(redis, session_factory, runtime=None, *args, **kwargs):
        try:
            await asyncio.Event().wait()  # ignores stop
        except asyncio.CancelledError:
            worker["scheduler_cancelled"] = True
            raise

    monkeypatch.setattr(worker_main, "run_scheduler", stuck_scheduler)
    before = signal.getsignal(signal.SIGTERM)
    async with _running_main() as main_task:
        await _until(lambda: worker["run_once"] > 0 and _replaced(signal.SIGTERM, before))
        started = time.monotonic()
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(asyncio.shield(main_task), 3)

    assert time.monotonic() - started < 1.0
    assert worker["scheduler_cancelled"] is True
    assert worker["redis"].closed is True


async def test_main_runs_without_signal_handler_support(worker, monkeypatch):
    loop_type = type(asyncio.get_running_loop())

    def unsupported(self, sig, callback, *args):
        raise NotImplementedError

    monkeypatch.setattr(loop_type, "add_signal_handler", unsupported)

    async def self_stopping_scheduler(redis, session_factory, runtime=None, *args, **kwargs):
        await asyncio.sleep(0.05)
        kwargs["stop"].set()

    monkeypatch.setattr(worker_main, "run_scheduler", self_stopping_scheduler)
    await asyncio.wait_for(worker_main.main(), 3)

    assert worker["redis"].closed is True


def test_shutdown_grace_constant():
    assert worker_main.SHUTDOWN_GRACE_SECONDS == 90
