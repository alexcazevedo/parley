---
id: 03
status: doing
model: sonnet
depends_on: [02]
max_attempts: 3
files:
  - app/channels/__init__.py
  - app/channels/base.py
  - app/channels/registry.py
  - app/channels/webchat.py
  - app/queue/__init__.py
  - app/queue/base.py
  - app/queue/redis_queue.py
  - app/messages/__init__.py
  - app/messages/ingest.py
  - app/conversations/__init__.py
  - app/conversations/resolver.py
  - app/api/webchat.py
  - app/api/dev.py
  - app/api/conversations.py
  - app/workers/main.py
  - app/main.py
  - tests/test_ingest.py
---
## Goal
Inbound message path end-to-end: Web Chat REST + dev simulator → normalize → idempotent persist → contact/conversation resolve → Redis queue → worker dequeues and logs.

## Context
`channels/base.py`: `IncomingMessage(tenant_id, channel_id, external_user_id, external_message_id, type, content, timestamp, raw)`, `OutgoingMessage(conversation_id, type='text', content)`, `ChannelAdapter` ABC with `parse_incoming(payload) -> IncomingMessage`, `send_message(OutgoingMessage, contact, channel)`, `download_media` (raise NotImplemented). `registry.py` maps `Channel.type` → adapter. `WebChatAdapter.send_message` persists only (client polls).

`POST /channels/webchat/messages` (tenant key) body `{channel_id, user_id, message, message_id?}`; if `message_id` absent generate uuid. `POST /dev/messages` body `{channel_id, user_id, message}` — thin wrapper on same ingest fn. `GET /conversations/{id}/messages?after=<iso>`.

`messages/ingest.py::ingest(session, queue, incoming)`: resolve contact+conversation (`conversations/resolver.py`: find-or-create contact, find open conversation not CLOSED else create ACTIVE_AI), insert Message (`ON CONFLICT DO NOTHING` on external id → return `duplicate=True`, no enqueue), `queue.enqueue(Job(conversation_id, message_id, attempt=0))`.

`queue/base.py`: `Queue` ABC `enqueue/dequeue(timeout)/ack/fail`. `RedisQueue`: `LPUSH harness:jobs`, `BRPOP`; `fail` re-enqueues with `attempt+1` up to `MAX_ATTEMPTS=3`, then `LPUSH harness:dlq`. Worker `main.py`: loop dequeue → log → ack (processing arrives in ticket 04).

## Checks (run in order — cheapest first)
1. `uv run ruff check .`
2. `uv run pytest -q tests/test_ingest.py`
3. `uv run pytest -q`

## Done when
- Same `message_id` posted twice → one Message row, one job.
- Two different users → two conversations; same user twice → one conversation.
- Job with 3 failures lands in `harness:dlq`.

## Not included
- Debounce, agent, WhatsApp, outbound sending logic.
