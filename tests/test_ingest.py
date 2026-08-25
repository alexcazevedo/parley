"""Ingestion pipeline: Web Chat + dev simulator -> idempotent persist ->
contact/conversation resolve -> Redis queue -> worker dequeue/log/ack.
"""

import os
import uuid

from app.queue.base import Job
from app.queue.redis_queue import DLQ_KEY, JOBS_KEY, MAX_ATTEMPTS, RedisQueue
from app.workers.main import run_once


def _admin_key() -> str:
    return os.environ["ADMIN_API_KEY"]


async def _create_tenant(client, name: str = "Acme") -> dict:
    response = await client.post(
        "/tenants", json={"name": name}, headers={"X-Admin-Key": _admin_key()}
    )
    assert response.status_code == 201
    return response.json()


def _auth(api_key: str) -> dict:
    return {"X-API-Key": api_key}


async def _create_channel(client, headers: dict) -> str:
    agent = await client.post(
        "/agents",
        json={"name": "Bot", "instructions": "Hi.", "model": "gpt-4o-mini"},
        headers=headers,
    )
    channel = await client.post(
        "/channels",
        json={"type": "webchat", "name": "Web", "agent_id": agent.json()["id"]},
        headers=headers,
    )
    return channel.json()["id"]


async def test_duplicate_message_id_inserts_once_and_enqueues_once(client, fake_redis):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_channel(client, headers)

    body = {"channel_id": channel_id, "user_id": "user-1", "message": "hi", "message_id": "m-1"}

    first = await client.post("/channels/webchat/messages", json=body, headers=headers)
    assert first.status_code == 201
    assert first.json()["duplicate"] is False

    second = await client.post("/channels/webchat/messages", json=body, headers=headers)
    assert second.status_code == 201
    assert second.json()["duplicate"] is True
    assert second.json()["message_id"] == first.json()["message_id"]

    conversation_id = first.json()["conversation_id"]
    messages = await client.get(f"/conversations/{conversation_id}/messages", headers=headers)
    assert len(messages.json()) == 1

    assert await fake_redis.llen(JOBS_KEY) == 1


async def test_two_users_get_two_conversations_same_user_reuses_one(client, fake_redis):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_channel(client, headers)

    a1 = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "user-a", "message": "hi"},
        headers=headers,
    )
    a2 = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "user-a", "message": "again"},
        headers=headers,
    )
    b1 = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": channel_id, "user_id": "user-b", "message": "hi"},
        headers=headers,
    )

    assert a1.status_code == 201
    assert b1.status_code == 201
    assert a1.json()["conversation_id"] == a2.json()["conversation_id"]
    assert a1.json()["conversation_id"] != b1.json()["conversation_id"]

    assert await fake_redis.llen(JOBS_KEY) == 3


async def test_dev_messages_uses_same_pipeline(client):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])
    channel_id = await _create_channel(client, headers)

    response = await client.post(
        "/dev/messages",
        json={"channel_id": channel_id, "user_id": "user-dev", "message": "hello"},
        headers=headers,
    )
    assert response.status_code == 201
    assert response.json()["duplicate"] is False


async def test_unknown_channel_returns_404(client):
    tenant = await _create_tenant(client)
    headers = _auth(tenant["api_key"])

    response = await client.post(
        "/channels/webchat/messages",
        json={"channel_id": str(uuid.uuid4()), "user_id": "user-1", "message": "hi"},
        headers=headers,
    )
    assert response.status_code == 404


async def test_worker_run_once_dequeues_logs_and_acks(fake_redis):
    queue = RedisQueue(fake_redis)
    job = Job(conversation_id=uuid.uuid4(), message_id=uuid.uuid4(), attempt=0)
    await queue.enqueue(job)

    result = await run_once(queue, timeout=1)
    assert result is not None
    assert result.conversation_id == job.conversation_id
    assert result.message_id == job.message_id
    assert await fake_redis.llen(JOBS_KEY) == 0


async def test_run_once_returns_none_on_empty_queue(fake_redis):
    queue = RedisQueue(fake_redis)
    result = await run_once(queue, timeout=1)
    assert result is None


async def test_job_lands_in_dlq_after_max_attempts(fake_redis):
    queue = RedisQueue(fake_redis)
    initial = Job(conversation_id=uuid.uuid4(), message_id=uuid.uuid4(), attempt=0)
    await queue.enqueue(initial)

    for _ in range(MAX_ATTEMPTS):
        job = await queue.dequeue(timeout=1)
        assert job is not None
        await queue.fail(job)

    assert await fake_redis.llen(JOBS_KEY) == 0
    assert await fake_redis.llen(DLQ_KEY) == 1

    dlq_raw = await fake_redis.lrange(DLQ_KEY, 0, -1)
    dead_job = Job.from_json(dlq_raw[0])
    assert dead_job.attempt == MAX_ATTEMPTS
    assert dead_job.conversation_id == initial.conversation_id
