---
id: 04
status: doing
model: opus
depends_on: [03]
max_attempts: 3
files:
  - app/conversations/debounce.py
  - app/conversations/lock.py
  - app/conversations/turn.py
  - app/workers/main.py
  - app/workers/scheduler.py
  - tests/test_debounce.py
---
## Goal
Per-conversation debounce + lock so N rapid messages become one `process_turn` call, safe across multiple workers.

## Context
Redis keys (`redis>=5` asyncio client):
- `harness:pending:{conv_id}` — LIST of message ids
- `harness:due` — ZSET conv_id → unix ts

Worker (`main.py`) on job: `RPUSH pending`, `ZADD due conv now+DEBOUNCE_SECONDS` (always overwrite → window extends on each message), ack.

`scheduler.py` (runs in same worker process as asyncio task): every 250ms `ZRANGEBYSCORE due -inf now LIMIT 0 50`; for each conv: `ZREM` only if `ZREM` returns 1 (claims it), then `lock.acquire(conv_id)` (`SET harness:lock:{id} <token> NX PX 60000`); if not acquired, `ZADD due conv now+1` and skip. On lock: `LRANGE pending 0 -1` + `DEL` atomically (Lua or MULTI), load Messages by id ordered by `created_at`, call `turn.process_turn(session, conv_id, messages)`. `finally`: release lock via compare-token Lua. If pending non-empty after processing (new msgs arrived), re-ZADD due now.

`turn.py`: `process_turn` joins message contents with `"\n"` into `input_text`; calls `runtime: TurnRuntime` protocol (`async run(conv, input_text) -> str`). Ship `EchoRuntime` default now; persists outbound Message role=assistant and calls `adapter.send_message`. Skip agent entirely if `conversation.state != ACTIVE_AI` (still drains pending).

Tests use `fakeredis.aioredis`; set `DEBOUNCE_SECONDS=0.2`.

## Checks (run in order — cheapest first)
1. `uv run ruff check .`
2. `uv run pytest -q tests/test_debounce.py`
3. `uv run pytest -q`

## Done when
- 3 messages within 200ms → exactly one `process_turn` with `"Oi\nQueria saber onde está\nmeu pedido 12345"`.
- Two schedulers on one conv → one processes, other requeues; lock released after.
- Message in HUMAN_HANDOFF conv → no assistant message created.

## Not included
- LLM, tools, memory. Echo only.
