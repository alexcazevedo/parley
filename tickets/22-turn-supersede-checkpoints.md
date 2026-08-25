---
id: 22
status: done
model: opus
depends_on: []
max_attempts: 3
files: [app/models/agent_run.py, migrations/versions/0007_agent_run_superseded.py, app/conversations/turn.py, app/agents/runtime.py, tests/test_barge_in_turn.py]
---
## Goal
`process_turn` accepts an injected "newer inbound arrived?" probe, checks it at exactly two safe checkpoints (before delivery, between humanizer bubbles), and on a hit drops unsent parts, marks the `AgentRun` `superseded`, and returns a `Superseded` outcome instead of a reply.

## Context
Read first: PRODUCT.md (barge-in), app/conversations/turn.py (`process_turn`,
`_deliver`, `_deliver_humanized`, `_maybe_escalate_on_sentiment`),
app/agents/runtime.py `run_turn` (creates the `AgentRun`, status set inside
`_loop`), app/models/agent_run.py, migrations/versions/0006_sentiment_flag.py
(header/style), tests/test_sentiment_escalation.py + tests/test_turn_humanize.py
(`RecordingAdapter`, patched `asyncio.sleep`, `_setup` helpers — mirror them).

Model + migration:
- `AgentRunStatus.SUPERSEDED = "superseded"`.
- `migrations/versions/0007_agent_run_superseded.py` (revises 0006): in
  `upgrade`, `with op.get_context().autocommit_block(): op.execute("ALTER TYPE
  agent_run_status ADD VALUE IF NOT EXISTS 'superseded'")` (ADD VALUE cannot
  run inside a transaction). `downgrade` is a no-op with a comment (Postgres
  cannot drop enum values). Tests use `create_all`, so the model enum alone
  covers them; the migration is for the compose stack.

Turn API (`app/conversations/turn.py`):
- New keyword-only param `supersede_check: SupersedeCheck | None = None`
  where `SupersedeCheck = Callable[[], Awaitable[bool]]`. `None` = today's
  behavior, byte-for-byte.
- New frozen dataclass `Superseded(delivered_parts: int)`; `process_turn`
  returns `Message | Superseded | None`.
- Probe helper `_is_superseded(check) -> bool`: awaits `check()`; any
  exception → log `turn.supersede_check_failed` (warning, exc_info) and
  return `False` (degrade to today's behavior).
- Checkpoint 1 — after the runtime returns, BEFORE `_deliver`: if
  superseded → `await _mark_run_superseded(session, conversation)`,
  `await session.commit()` (persists the AgentRun + tool messages the runtime
  wrote; observable, not lost), log `turn.superseded` with
  `delivered_parts=0`, return `Superseded(0)`. Skip `_deliver`,
  `_maybe_summarize`, and `last_message_at`.
- Checkpoint 2 — inside `_deliver_humanized`, at the top of every loop
  iteration EXCEPT the first (the first part is covered by checkpoint 1):
  if superseded → stop the loop; already-sent parts stand (they are already
  committed per part). `_deliver_humanized` must report back how many parts
  it sent and whether it stopped early — return a small result (e.g.
  `tuple[Message | None, int, bool]` or a dataclass); `_deliver` adapts.
  `process_turn` then does the same mark+commit+log as checkpoint 1 and
  returns `Superseded(n)`.
- Non-humanized path: only checkpoint 1 applies (single send, nothing to
  cut). Sentiment-escalation path (`_maybe_escalate_on_sentiment`): NO
  checkpoints — the conversation is already `HUMAN_HANDOFF`, the
  `handoff_message` must go out whole. Not-`ACTIVE_AI` early return is
  untouched.
- `_mark_run_superseded(session, conversation)`: the newest `AgentRun` for
  this conversation in the session (`select(AgentRun).where(conversation_id
  ==).order_by(AgentRun.created_at.desc(), AgentRun.id).limit(1)` — the row
  is flushed, so it is visible inside the transaction) gets
  `status = SUPERSEDED`. No run (EchoRuntime / test doubles) → no-op.
  Do NOT overwrite an `ERROR` run (keep the error; a superseded errored
  turn is still an error) — only `SUCCESS`/`RUNNING` flip.
- Remove the stale comment in app/agents/runtime.py saying the enum "must
  not be touched" only if you change that file; otherwise leave runtime.py
  alone (`max_iterations` marker stays).

Tests `tests/test_barge_in_turn.py` — call `process_turn` directly with a
scripted `supersede_check` (a list of bools consumed in order, records call
count), `RecordingAdapter`, patched sleep, humanized channel with
`MULTI_PART_REPLY` (3 parts) and a runtime that returns it:
(a) `check=None` → 3 parts sent, Message returned, check never consulted;
(b) `[True]` → 0 sends, returns `Superseded(0)`, no assistant Message rows,
`AgentRun` (when using `AgentRuntime` + `FakeLLMProvider`) status
`superseded` and committed; (c) `[False, True]` → exactly 1 part sent and
persisted, returns `Superseded(1)`, run `superseded`; (d) `[False, False,
False]` → all 3 sent, Message returned, check called exactly 3 times;
(e) non-humanized channel, `[True]` → no send, `Superseded(0)`; (f) check
raises → full reply delivered as if `None`, warning logged; (g) sentiment
escalation trip (monkeypatch `turn.score_frustration` → 0.9) with a
`[True, True, True]` check → handoff_message delivered in full, check never
called; (h) runtime error path (`AgentRuntime` with a provider that raises)
+ `[True]` → run stays `error`, not `superseded`.

## Checks (run in order — cheapest first)
1. `uv run ruff check app tests`
2. `uv run python -c "import app.main"`
3. `docker compose up -d postgres && uv run pytest tests/test_barge_in_turn.py -q`
4. `uv run pytest -q`
5. `docker compose up -d --build api && sleep 6 && docker compose exec -T postgres psql -U harness -d harness -tAc "select enum_range(null::agent_run_status)" | grep superseded`

## Done when
- Supersede at each checkpoint proven by tests; suite green; migration
  applied on the compose stack shows `superseded` in the enum.

## Not included
- Redis probe / scheduler wiring / requeue of the old batch (ticket 23);
  mid-LLM or mid-tool cancellation; retracting sent bubbles; docs/README.
