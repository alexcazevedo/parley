---
id: 18
status: done
model: sonnet
depends_on: []
max_attempts: 3
files: [app/models/handoff.py, migrations/versions/0005_handoff_summary.py, app/handoff/summarizer.py, app/tools/builtin.py, app/api/conversations.py, tests/test_handoff_summary.py]
---
## Goal
Every handoff (agent-tool or operator) gets a best-effort LLM-written operator brief persisted on the `Handoff` row; LLM failure never blocks the escalation.

## Context
Read first: PRODUCT.md, app/handoff/service.py (keep it PURE — do not add
LLM calls there), app/tools/builtin.py `_handle_handoff` (ToolContext has
`.session`, `.agent`, `.conversation_id`), app/api/conversations.py `POST
/{id}/handoff`, app/conversations/summary.py + app/agents/context.py (how
existing code loads recent messages/memories and calls the provider),
app/llm/factory.py `get_provider(agent)`, tests/test_handoff.py +
tests/conftest.py (fixtures; tests use `create_all`, so the model column is
enough for tests — the migration is for real DBs).

Build:
- `app/models/handoff.py`: add `summary: Mapped[str | None] =
  mapped_column(Text, nullable=True)`.
- `migrations/versions/0005_handoff_summary.py`: alembic revision (mirror
  0004's structure/naming, down_revision = 0004's id) adding nullable
  `summary` Text to `handoffs`.
- `app/handoff/summarizer.py`: `async def generate_handoff_summary(session,
  conversation, reason) -> str | None`. Loads agent, recent messages
  (last ~30, same repo the context builder uses), contact memories,
  `conversation.summary`; one chat-completion via `get_provider(agent)`
  (no tools) with a prompt asking for a compact operator brief in the
  conversation's language: who the contact is, what they want, what the
  agent already did/answered incl. tool results, why it escalated, current
  tone. Wrap the whole thing in `asyncio.wait_for(..., timeout=10)` +
  try/except Exception → log via structlog, return None. Also export
  `async def handoff_with_summary(session, conversation, reason,
  requested_by) -> Handoff`: calls `HandoffService.handoff(...)`, then sets
  `record.summary = await generate_handoff_summary(...)`, flushes, returns
  record.
- Switch both callsites to `handoff_with_summary`: `_handle_handoff` in
  app/tools/builtin.py and `POST /conversations/{id}/handoff` in
  app/api/conversations.py (keep IllegalTransitionError handling exactly
  as is at both sites; endpoint still commits as today).

Tests `tests/test_handoff_summary.py` (existing fixtures + scripted/fake
LLM provider pattern from the suite): (a) agent-tool handoff persists a
non-null summary; (b) operator endpoint handoff persists a summary; (c)
provider raising → handoff still transitions to HUMAN_HANDOFF, Handoff row
exists, summary is None; (d) summarizer prompt receives the handoff reason
(assert via scripted provider's captured request).

## Checks (run in order — cheapest first)
1. `uv run ruff check app tests`
2. `uv run python -c "import app.main"`
3. `docker compose up -d postgres && uv run pytest tests/test_handoff_summary.py -q`
4. `uv run pytest -q`  (full suite regression)
5. `uv run alembic upgrade head` fails without a running target DB from host — instead verify migration applies inside compose: `docker compose up -d --build api && sleep 6 && docker compose logs api 2>&1 | grep -i "error" | grep -iv "errors=" || true` then `docker compose exec -T postgres psql -U harness -d harness -tc "\d handoffs" | grep summary`

## Done when
- Both handoff paths persist a brief; LLM failure path proven harmless;
  `handoffs.summary` column exists in the compose DB after migration.

## Not included
- API exposure of the summary (ticket 19), docs, operator UI, re-summarize
  on resume, redaction.
