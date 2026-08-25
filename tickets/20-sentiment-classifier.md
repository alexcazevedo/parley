---
id: 20
status: done
model: sonnet
depends_on: []
max_attempts: 3
files: [app/agents/sentiment.py, app/config.py, app/models/agent.py, migrations/versions/0006_sentiment_flag.py, app/api/chat.py, tests/test_sentiment.py]
---
## Goal
Frustration classifier module + per-agent enable flag (column, migration, demo bootstrap), fully unit-tested; no turn wiring yet.

## Context
Read first: PRODUCT.md, app/llm/factory.py (`get_provider(agent)`) and one
existing provider call site (app/handoff/summarizer.py is the closest
pattern — best-effort LLM call with timeout), app/models/agent.py,
migrations/versions/0005_handoff_summary.py (revision structure to mirror),
app/api/chat.py bootstrap, tests/test_handoff_summary.py (scripted-provider
test pattern).

Build:
- `app/agents/sentiment.py`: `async def score_frustration(session,
  conversation, agent, input_text: str) -> float | None`. Loads the last
  ~10 conversation messages for context (same repo other code uses); one
  chat call via `get_provider(agent)`, prompt: rate USER frustration 0.0–1.0
  considering escalation over the conversation, reply with ONLY the number.
  Parse defensively: extract first float in the response, clamp to [0,1];
  unparseable → None. Whole call in `asyncio.wait_for(...,
  settings.sentiment_timeout_seconds)`; any exception → structlog warning +
  None.
- `app/config.py`: `sentiment_threshold: float = 0.7`,
  `sentiment_timeout_seconds: float = 5.0`.
- `app/models/agent.py`: `sentiment_escalation_enabled: Mapped[bool] =
  mapped_column(Boolean, nullable=False, default=False)` (+ server_default
  sa.false() in the migration so existing rows are valid).
- `migrations/versions/0006_sentiment_flag.py`: add the column,
  down_revision = "0005"-revision id (read 0005 for the exact string). NO
  enum change — `handoff_requested_by` already contains 'system' (0002).
- `app/api/chat.py` bootstrap: demo agent created with
  `sentiment_escalation_enabled=True`; on the existing-tenant path, load
  the demo channel's agent and set the flag to True if False (plain bool
  column — simple setattr + commit; NOT the JSONB pitfall).

Tests `tests/test_sentiment.py`: scripted provider returning "0.85" →
0.85; "Score: 0.9/1" → parses 0.9; garbage → None; provider raising →
None; timeout (provider sleeps past a tiny patched timeout) → None; result
clamped for "1.7"; bootstrap sets/upgrades the agent flag (extend pattern
from tests/test_bootstrap.py if simpler — but keep the test in
tests/test_sentiment.py).

## Checks (run in order — cheapest first)
1. `uv run ruff check app tests`
2. `uv run python -c "import app.main"`
3. `docker compose up -d postgres && uv run pytest tests/test_sentiment.py -q`
4. `uv run pytest -q`  (full suite, 129 currently green)
5. `docker compose up -d --build api && sleep 6 && docker compose exec -T postgres psql -U harness -d harness -tc "\d agents" | grep sentiment_escalation_enabled`

## Done when
- Classifier returns clamped floats / None per the cases above; column
  migrated in compose DB; demo agent flag true after bootstrap.

## Not included
- Turn integration, handoff, delivery, docs (ticket 21). No score
  persistence anywhere.
