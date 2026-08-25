---
id: 23
status: done
model: sonnet
depends_on: [22]
max_attempts: 3
files: [app/workers/scheduler.py, app/conversations/debounce.py, tests/test_barge_in_scheduler.py, tests/test_debounce.py, README.md, docs/ARCHITECTURE.md]
---
## Goal
The scheduler feeds `process_turn` a Redis-backed supersede probe and, on a `Superseded` outcome, puts the old batch back on the pending list so the conversation re-runs promptly with old + new messages in one turn.

## Context
Read first: PRODUCT.md (barge-in), app/workers/scheduler.py (`tick`),
app/conversations/debounce.py, app/conversations/turn.py (ticket 22:
`supersede_check`, `Superseded`), tests/test_debounce.py (pipeline helpers
`_setup_channel`/`_send`/`_drain_jobs`/`_assistant_messages`,
`RecordingRuntime`, and `test_message_arriving_during_turn_requeues_conversation`),
tests/test_turn_humanize.py (`RecordingAdapter`, patched sleep, humanized
channel setup).

Signal = the existing pending list: `drain_pending` empties it at claim
time, so any item on it during the turn is a newer inbound message. No new
transport.

`app/conversations/debounce.py`: add `requeue_pending(redis,
conversation_id, message_ids)` — `LPUSH` the ids in REVERSE order so the
old batch ends up at the head, ahead of the interrupting messages (order in
the list is arrival order; `_load_messages` re-sorts by `created_at`
anyway). Skip on empty list.

`app/workers/scheduler.py::tick`:
- Build `check = lambda: debounce.has_pending(redis, raw_conversation_id)`
  (an async callable) and pass `supersede_check=check` to `process_turn`.
- If the result is a `Superseded`: `await debounce.requeue_pending(redis,
  raw_conversation_id, message_ids)`, log `scheduler.turn_superseded` with
  `delivered_parts`, and do NOT append to `processed`. Requeue happens inside
  the `try` (before lock release) so a crash between turn and requeue is
  the only way to lose the batch — same exposure as today.
- The existing tail (`has_pending` → `schedule(now)`) then re-arms the
  conversation immediately; keep it as is. Next tick drains old + new ids
  into one batch — one reply for everything.
- Non-`Superseded` results: unchanged.

Update `tests/test_debounce.py::test_message_arriving_during_turn_requeues_conversation`
to the new contract: the mid-turn push now supersedes the (non-humanized,
`RecordingRuntime`) turn, so assert: runtime called once with "Oi", NO
assistant message persisted, conversation re-armed in `DUE_KEY`, pending
list holds 2 ids with the "Oi" message id FIRST. (The random uuid it pushes
is fine for this test.)

Tests `tests/test_barge_in_scheduler.py` (full pipeline: `_send` →
`_drain_jobs` → `tick`; humanized channel; runtime = `AgentRuntime` +
`FakeLLMProvider` scripted with two responses, or a recording runtime that
returns `MULTI_PART_REPLY`; `RecordingAdapter`; patched sleep whose fake
sleep can `_send` + `_drain_jobs` the interrupting message on a chosen
part index so the pending list is non-empty at checkpoint 2):
(a) interrupt after part 1 → tick 1 returns `[]`, 1 assistant message
persisted, `AgentRun` superseded; tick 2 (now+1) → runtime's second
`input_text` contains BOTH the original text and the interrupting text,
one new reply set delivered, exactly one `success` run; no third turn
(tick 3 returns `[]`, pending empty);
(b) no-lost-message guarantee: every inbound `Message` id in the DB appears
in some runtime `input_text` across ticks (assert set equality);
(c) never-two-replies: total assistant messages == parts sent before cut +
parts of the final reply, no reply text duplicated;
(d) interrupt during the LLM call (push inside the runtime, like the
debounce test) on a humanized channel → 0 parts sent, `Superseded(0)`,
tick 2 answers the merged batch;
(e) `HUMAN_HANDOFF` conversation + pending message → today's behavior
(turn skipped, no requeue of the batch, `process_turn` returns `None`);
(f) probe failure (monkeypatch `debounce.has_pending` to raise) →
reply delivered in full, conversation still re-armed by the tail
`has_pending` if it recovers — assert delivery only.

Docs: README — one bullet under "Architecture (at a glance)" or the
humanizer mention: "barge-in: a message arriving mid-reply supersedes the
turn at safe checkpoints; the conversation re-runs with the full batch";
remove/adjust nothing else. docs/ARCHITECTURE.md — in "Queue / debounce
algorithm" step 3 add the supersede probe + requeue; new short "## Barge-in"
subsection after "Humanizer": checkpoints (before delivery, between
bubbles), no mid-call abort, sent bubbles stand, `AgentRunStatus.SUPERSEDED`,
requeue → one merged reply, degrade-to-today on probe failure, escalation
path exempt.

## Checks (run in order — cheapest first)
1. `uv run ruff check app tests`
2. `uv run python -c "import app.main"`
3. `docker compose up -d postgres && uv run pytest tests/test_barge_in_scheduler.py tests/test_debounce.py -q`
4. `uv run pytest -q`
5. Live barge-in probe (stack rebuilt: `docker compose up -d --build api worker && sleep 6`; real LLM key in .env): bootstrap via `/chat` → send "Oi, onde está meu pedido 12345?" → wait ~4s (debounce + first bubble) → send "na verdade é o pedido 99" → wait ~25s → `GET /conversations/{conv}/messages` (X-API-Key: demo-webchat-key): assistant messages after the second user message must mention 99, none of them mention 12345 as the answered order except bubbles created before the interrupt, and no assistant message text appears twice; `select status from agent_runs where conversation_id=...` shows one `superseded` and one `success`.

## Done when
- Live probe: stale bubbles stop, a single fresh reply addresses order 99,
  nothing answers twice; guarantees (no lost message, no double reply,
  handoff untouched, probe failure degrades) proven by tests; docs updated.

## Not included
- Webchat page changes; "user is typing" pre-emption; cross-conversation
  semantics; retracting sent bubbles; operator UI.
