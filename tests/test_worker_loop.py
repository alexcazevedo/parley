"""The worker's job loop survives Redis errors and still stops on request."""

import asyncio
import uuid

import pytest
from structlog.testing import capture_logs

from app.conversations import debounce
from app.queue.base import Job
from app.queue.redis_queue import RedisQueue
from app.workers import main as worker_main
from app.workers.main import job_loop


class FlakyQueue(RedisQueue):
    """A real queue whose first `dequeue` calls raise."""

    def __init__(self, redis, failures: int) -> None:
        super().__init__(redis, worker_id="w1")
        self.failures = failures
        self.calls = 0

    async def dequeue(self, timeout: int = 5) -> Job | None:
        self.calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("redis is restarting")
        return await super().dequeue(timeout=min(timeout, 1))


@pytest.fixture(autouse=True)
def _short_pause(monkeypatch):
    monkeypatch.setattr(worker_main, "JOB_LOOP_ERROR_PAUSE_SECONDS", 0.01)


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(0.01)


async def test_loop_survives_dequeue_errors_and_handles_the_job(fake_redis):
    queue = FlakyQueue(fake_redis, failures=2)
    job = Job(conversation_id=uuid.uuid4(), message_id=uuid.uuid4())
    await queue.enqueue(job)
    stop = asyncio.Event()

    with capture_logs() as logs:
        task = asyncio.create_task(job_loop(queue, fake_redis, stop, 0.1))
        try:

            async def pending() -> bool:
                return await debounce.has_pending(fake_redis, job.conversation_id)

            await _wait_until(pending)
            assert not task.done()
        finally:
            stop.set()
            await asyncio.wait_for(task, timeout=5)

    errors = [e for e in logs if e["event"] == "worker.job_loop_error"]
    assert len(errors) == 2
    assert queue.calls >= 3
    assert await debounce.drain_pending(fake_redis, job.conversation_id) == [job.message_id]


async def test_stop_during_error_pause_ends_the_loop_promptly(fake_redis, monkeypatch):
    monkeypatch.setattr(worker_main, "JOB_LOOP_ERROR_PAUSE_SECONDS", 30.0)
    queue = FlakyQueue(fake_redis, failures=1000)
    stop = asyncio.Event()
    task = asyncio.create_task(job_loop(queue, fake_redis, stop, 0.1))

    async def failed_once() -> bool:
        return queue.calls >= 1

    await _wait_until(failed_once)
    await asyncio.sleep(0.05)  # now inside the pause
    assert not task.done()
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert queue.calls == 1


async def test_loop_pauses_between_errors_instead_of_spinning(fake_redis, monkeypatch):
    monkeypatch.setattr(worker_main, "JOB_LOOP_ERROR_PAUSE_SECONDS", 0.2)
    queue = FlakyQueue(fake_redis, failures=1000)
    stop = asyncio.Event()
    task = asyncio.create_task(job_loop(queue, fake_redis, stop, 0.1))
    await asyncio.sleep(0.5)
    stop.set()
    await asyncio.wait_for(task, timeout=2)
    assert 2 <= queue.calls <= 4


async def test_cancellation_is_not_swallowed(fake_redis):
    queue = FlakyQueue(fake_redis, failures=0)
    stop = asyncio.Event()
    task = asyncio.create_task(job_loop(queue, fake_redis, stop, 0.1))
    await asyncio.sleep(0.05)  # blocked in dequeue
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)


async def test_cancellation_during_error_pause_is_not_swallowed(fake_redis, monkeypatch):
    monkeypatch.setattr(worker_main, "JOB_LOOP_ERROR_PAUSE_SECONDS", 30.0)
    queue = FlakyQueue(fake_redis, failures=1000)
    stop = asyncio.Event()
    task = asyncio.create_task(job_loop(queue, fake_redis, stop, 0.1))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=2)
