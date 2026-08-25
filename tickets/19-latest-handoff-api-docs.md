---
id: 19
status: done
model: sonnet
depends_on: [18]
max_attempts: 3
files: [app/api/conversations.py, tests/test_latest_handoff_api.py, README.md, docs/ARCHITECTURE.md]
---
## Goal
`GET /conversations/{id}` exposes `latest_handoff` (reason, summary, requested_by, created_at) so operators read the brief; docs updated.

## Context
Ticket 18 added `Handoff.summary` and both handoff paths fill it. Read:
app/api/conversations.py (`ConversationOut`, `get_conversation`),
app/handoff/service.py `_latest_open_handoff` (query shape to mirror —
but here: latest handoff regardless of resolved state, newest
`created_at` first), app/models/handoff.py.

Build in app/api/conversations.py:
- `class HandoffOut(BaseModel)`: `reason: str | None`, `summary: str |
  None`, `requested_by: HandoffRequestedBy`, `created_at: datetime`
  (`model_config = ConfigDict(from_attributes=True)` as the file does).
- `ConversationOut` gains `latest_handoff: HandoffOut | None = None`.
- `get_conversation` (single-conversation GET only — NOT the list
  endpoint) queries the newest Handoff for the conversation and populates
  the field. List endpoint keeps returning conversations without it
  (avoids N+1); note that in a comment.

Docs:
- `docs/ARCHITECTURE.md`: extend the handoff section — warm-handoff brief:
  generated at handoff time (both paths), best-effort with timeout, stored
  on `handoffs.summary`, surfaced via `latest_handoff` on single-GET;
  pointer to app/handoff/summarizer.py.
- `README.md`: one sentence in the feature blurb.

Tests `tests/test_latest_handoff_api.py`: (a) conversation with no
handoffs → `latest_handoff` null; (b) after a handoff with summary → GET
returns populated object; (c) two handoffs → newest one returned.

## Checks (run in order — cheapest first)
1. `uv run ruff check app tests`
2. `uv run python -c "import app.main"`
3. `docker compose up -d postgres && uv run pytest tests/test_latest_handoff_api.py -q`
4. `uv run pytest -q`
5. Live probe (real LLM key in .env, stack up): `docker compose up -d --build api worker && sleep 6` then bootstrap + send a message via `POST /channels/webchat/messages` (X-API-Key: demo-webchat-key, channel from `POST /chat/bootstrap`), wait ~15s, `POST /conversations/{conv}/handoff` with a reason, then `GET /conversations/{conv}` must show `latest_handoff.summary` non-null.

## Done when
- Live GET shows an LLM-written brief after an operator handoff; suite
  green.

## Not included
- latest_handoff on the list endpoint, operator UI, webhooks, webchat page
  changes.
