---
id: 16
status: done
model: opus
depends_on: [14, 15]
max_attempts: 3
files: [app/conversations/turn.py, app/agents/runtime.py, app/agents/context.py, tests/test_turn_humanize.py]
---
## Goal
Wire the humanizer into `process_turn`: when `channel.config.humanize` is true, mark read → split reply → per part: typing presence + paced delay + persist + send + commit — staggered so polling clients see bubbles appear over time.

## Context
Read first: `app/conversations/turn.py` (current single-reply flow),
`app/conversations/humanize.py` + `app/config.py` humanize settings
(ticket 14), `app/channels/base.py` hooks (ticket 15),
`app/agents/runtime.py`, `app/agents/context.py`.

In `process_turn`:
- Load `channel` BEFORE running the runtime (it's currently loaded after).
  `humanized = bool((channel.config or {}).get("humanize"))`.
- If humanized: best-effort `adapter.mark_read(newest inbound
  external_message_id, contact, channel)` before the runtime call — wrap in
  try/except, log, never fail the turn (skip when the newest inbound
  message has no `external_message_id`).
- Humanized reply path (replaces the single persist+send when on): parts =
  `split_reply(reply_text, settings.humanize_max_parts)` (fall back to
  `[reply_text]` if empty). For each part: `delay =
  typing_delay_seconds(part, settings)`; best-effort
  `adapter.send_typing(contact, channel, delay)`; `await
  asyncio.sleep(delay)`; persist a Message row (same fields as today,
  content=part, explicit `created_at=datetime.now(UTC)`); `await
  adapter.send_message(...)` (best-effort per part after the first —
  first-part send failure keeps today's raise behavior); `await
  session.commit()` AFTER each part so pollers see bubbles staggered.
  Return the LAST part's Message. `humanize` off/absent → today's flow
  byte-for-byte (single persist, single send, one commit).
- Note: the first per-part commit also commits the runtime's AgentRun/tool
  rows — that is fine; `maybe_summarize` + `last_message_at` update happen
  after the loop as today.

Style hint: `ContextBuilder.build`/`_system_prompt` gain optional
`style_hint: str | None = None` appended as a final system-prompt part.
`AgentRuntime` (find where it calls the builder) fetches the conversation's
Channel and, when humanized, passes: "Style: write like a human chat agent —
short messages, split distinct thoughts into separate paragraphs with a
blank line between them; at most a few sentences per paragraph."

Tests `tests/test_turn_humanize.py` (mirror `tests/test_agent_loop.py` /
`tests/test_handoff.py` fixtures — real test Postgres + fakeredis): fake
adapter records call sequence; monkeypatch `asyncio.sleep` in the turn
module to record delays without sleeping. Assert: (a) humanize on + a
multi-paragraph runtime reply → mark_read once, then per part
send_typing→persist→send_message in order, N Message rows, delays clamped
per settings; (b) humanize off → exactly one Message row, no
mark_read/send_typing calls; (c) mark_read raising does not prevent the
reply.

## Checks (run in order — cheapest first)
1. `uv run ruff check app tests`
2. `uv run python -c "import app.main"`
3. `docker compose up -d postgres && uv run pytest tests/test_turn_humanize.py -q`
4. `uv run pytest -q`  (full suite — humanize-off regression)

## Done when
- Humanized turn emits staggered per-part commits in the documented order;
  suite green with humanize off everywhere else.

## Not included
- Demo/bootstrap wiring + docs (ticket 17); webchat page changes (its
  polling + typing indicator already render staggered parts); barge-in.
