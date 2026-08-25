---
id: 24
status: done
model: sonnet
depends_on: []
max_attempts: 3
files: [app/api/conversations.py, app/repositories/conversations.py, tests/test_operator_api.py]
---
## Goal
`GET /conversations` can serve an operator inbox in one round-trip: multi-state filter, newest-activity-first ordering, contact label, and (opt-in) the latest handoff brief per row without an N+1.

## Context
Read first: PRODUCT.md (operator UI), app/api/conversations.py
(`list_conversations`, `ConversationOut`, `HandoffOut`, `_latest_handoff`),
app/repositories/conversations.py + app/repositories/base.py (`list`),
app/models/conversation.py, app/models/contact.py (`external_user_id`,
`display_name`), app/models/handoff.py, tests/test_handoff.py
(`test_list_conversations_filters_by_state`, `_setup`/`_send`/`_drain_jobs`
helpers), tests/test_latest_handoff_api.py (handoff-with-summary fixture
pattern: patches the summarizer's provider with `FakeLLMProvider`).

Changes to `GET /conversations` — all additive, existing callers unaffected:
- `state` becomes repeatable: `state: list[ConversationState] | None =
  Query(default=None)`. `?state=HUMAN_HANDOFF` (single, today's usage) and
  `?state=ACTIVE_AI&state=WAITING` both work; no param = all states.
  Filter is `Conversation.state.in_(states)`.
- Ordering: `last_message_at DESC NULLS LAST, created_at DESC` (newest
  activity first). Implement in `ConversationsRepository.list_for_inbox(
  tenant_id, states: list[ConversationState] | None, limit: int)` — a new
  method, leave `BaseRepository.list` alone.
- `limit: int = Query(default=100, ge=1, le=500)`.
- `ConversationOut` gains two optional fields, populated by the list AND the
  single GET: `contact_external_user_id: str | None = None`,
  `contact_display_name: str | None = None`. Load contacts with ONE query
  (`select(Contact).where(Contact.id.in_(ids))`) and map in Python, or a
  join — either way exactly one extra statement for the whole list.
- `include_handoff: bool = Query(default=False)`. When true, populate
  `latest_handoff` for every row with ONE statement: Postgres
  `DISTINCT ON (conversation_id)` over `Handoff` filtered by tenant and the
  page's conversation ids, `ORDER BY conversation_id, created_at DESC`
  (`select(Handoff).distinct(Handoff.conversation_id).where(...).order_by(
  Handoff.conversation_id, Handoff.created_at.desc())`). Rows without a
  handoff keep `latest_handoff=None`. Default false keeps the documented
  no-N+1 behavior; update the comment in `list_conversations` accordingly.
- `MessageOut` unchanged. `HandoffOut` unchanged.

Tests `tests/test_operator_api.py` (reuse helper style from
tests/test_handoff.py; distinct `user_id`s create distinct conversations;
`_drain_jobs` before handoff calls): (a) multi-state filter
`?state=ACTIVE_AI&state=WAITING` returns only those; single `?state=` still
works; (b) ordering: three conversations, bump one's activity by sending a
later message → it comes first; a conversation with `last_message_at=None`
(set directly on the row in `db_session`) comes last; (c) `limit=2` returns
2; (d) `include_handoff=true` on a page with two handed-off conversations
(one with two handoffs — newest brief wins) and one never-handed-off →
briefs correct, `None` for the last, and the endpoint executes a bounded
number of statements (assert via a SQLAlchemy `before_cursor_execute`
listener counting statements on `db_session.bind` / the test engine, or
simpler: patch `_latest_handoff` to fail if called — it must not be used by
the list); (e) `include_handoff` omitted → `latest_handoff` is `null` for
every row (existing behavior, keep
tests/test_latest_handoff_api.py green); (f) `contact_external_user_id` /
`contact_display_name` present on list rows and single GET; (g) tenant
isolation: a second tenant's conversations never appear.

## Checks (run in order — cheapest first)
1. `uv run ruff check app tests`
2. `uv run python -c "import app.main"`
3. `docker compose up -d postgres && uv run pytest tests/test_operator_api.py tests/test_handoff.py tests/test_latest_handoff_api.py -q`
4. `uv run pytest -q`
5. `docker compose up -d --build api && sleep 6 && curl -sf "localhost:8001/conversations?state=HUMAN_HANDOFF&state=ACTIVE_AI&include_handoff=true&limit=5" -H "X-API-Key: demo-webchat-key" | python3 -c "import sys,json; rows=json.load(sys.stdin); assert isinstance(rows,list); assert all('latest_handoff' in r and 'contact_external_user_id' in r for r in rows); print('ok', len(rows))"`

## Done when
- One `GET /conversations?state=…&state=…&include_handoff=true` call returns
  everything the inbox and Ativas lists need, ordered newest-activity
  first, in a bounded number of SQL statements; suite green.

## Not included
- The `/operator` page itself (ticket 25); pagination cursors; search;
  unread counters; any change to handoff/resume/close/messages endpoints.
