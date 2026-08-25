---
id: 21
status: done
model: opus
depends_on: [20]
max_attempts: 3
files: [app/conversations/turn.py, tests/test_sentiment_escalation.py, README.md, docs/ARCHITECTURE.md]
---
## Goal
Wire the classifier into `process_turn`: frustrated `ACTIVE_AI` batch → skip agent, escalate via warm handoff (requested_by=system), deliver the agent's `handoff_message` through the normal (humanizer-aware) path.

## Context
Read first: app/conversations/turn.py (current flow: state check →
runtime → `_mark_read`/`_deliver_humanized`/`_build_reply` helpers),
app/agents/sentiment.py `score_frustration(session, conversation, agent,
input_text)` (ticket 20), app/handoff/summarizer.py
`handoff_with_summary(session, conversation, reason, requested_by)`,
app/models/handoff.py `HandoffRequestedBy.SYSTEM`,
tests/test_turn_humanize.py (recording-adapter + patched-sleep fixtures to
reuse/mirror).

In `process_turn`, after the `ACTIVE_AI` state check and BEFORE the
runtime call:
- Load the agent (`session.get(Agent, conversation.agent_id)`).
- If `agent is not None and agent.sentiment_escalation_enabled`:
  `score = await score_frustration(session, conversation, agent,
  input_text)` (input_text = the joined batch, already built). The scorer
  is already best-effort; treat `None` as "no signal".
- If `score >= settings.sentiment_threshold`: escalation path, in this
  order (state correctness over latency — accepted mvp tradeoff that the
  brief adds a few seconds before the reply):
  1. `await handoff_with_summary(session, conversation,
     reason=f"frustração do usuário detectada (score {score:.2f})",
     requested_by=HandoffRequestedBy.SYSTEM)`
  2. Deliver `agent.handoff_message` as the reply REUSING the existing
     delivery code (humanized branch incl. `_mark_read` behavior when on,
     plain persist+send otherwise) — refactor the existing
     persist/send/commit tail into a small helper if needed rather than
     duplicating it.
  3. Update `conversation.last_message_at`, commit, log
     `turn.sentiment_escalated` with score, return the reply Message.
     Skip `_maybe_summarize` and the runtime entirely.
- Otherwise: today's flow untouched. Flag disabled → scorer never called.

Tests `tests/test_sentiment_escalation.py` (monkeypatch
`turn.score_frustration`; runtime = recording fake that fails the test if
called): (a) score 0.9 → state HUMAN_HANDOFF, Handoff row
requested_by=system with reason containing "0.90", reply content ==
agent.handoff_message, runtime NOT called; (b) score 0.5 → normal turn,
runtime called, no new Handoff; (c) scorer returns None → normal turn;
(d) `sentiment_escalation_enabled=False` → scorer not called (patch
asserts zero calls); (e) humanized channel + score 0.9 → handoff_message
still delivered (parts ≥ 1, adapter send called).

Docs: README one sentence; docs/ARCHITECTURE.md short "Sentiment
escalation" subsection (classifier, threshold, skip-agent flow,
requested_by=system, best-effort guarantee).

## Checks (run in order — cheapest first)
1. `uv run ruff check app tests`
2. `uv run python -c "import app.main"`
3. `docker compose up -d postgres && uv run pytest tests/test_sentiment_escalation.py -q`
4. `uv run pytest -q`
5. Live rage probe (stack rebuilt: `docker compose up -d --build api worker && sleep 6`; real LLM key in .env): bootstrap → send "Oi, meu pedido 12345 nao chegou" → wait ~20s → send "isso nao resolve nada, que atendimento horrivel" → wait ~20s → `GET /conversations/{conv}` (X-API-Key: demo-webchat-key) must show state HUMAN_HANDOFF and latest_handoff.requested_by == "system" with non-null summary, and the last assistant-visible message is the agent's handoff_message.

## Done when
- Live rage probe passes end-to-end; suite green; calm/off/failure paths
  proven by tests.

## Not included
- Score persistence/analytics, threshold per tenant/channel, cooldowns,
  webchat page changes, barge-in.
