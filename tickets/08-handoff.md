---
id: 08
status: todo
model: sonnet
depends_on: [07]
max_attempts: 3
files:
  - app/handoff/__init__.py
  - app/handoff/service.py
  - app/tools/builtin.py
  - app/api/conversations.py
  - app/conversations/turn.py
  - tests/test_handoff.py
---
## Goal
Human handoff: agent/user/operator can flip a conversation to HUMAN_HANDOFF, AI goes silent, operator replies via API, resume/close endpoints.

## Context
`handoff/service.py::HandoffService`: `handoff(conv, reason, requested_by)` — ACTIVE_AI|WAITING → HUMAN_HANDOFF, insert `Handoff`; `resume(conv)` — HUMAN_HANDOFF → ACTIVE_AI, set `resolved_at`; `close(conv)` — any → CLOSED. Illegal transition → 409.

Built-in tool `handoff_to_human(reason)` in `builtin.py` (only if `agent.handoff_enabled`): calls service, returns `{ok: true}`; runtime then ends loop and sends `agent.handoff_message` (add column via migration `0004`, default "Vou te transferir para um atendente humano.").

Endpoints (tenant key):
- `GET /conversations?state=HUMAN_HANDOFF`
- `GET /conversations/{id}` (with contact, state, summary)
- `POST /conversations/{id}/messages` `{content}` → Message role=human_operator direction=out, sent via adapter
- `POST /conversations/{id}/handoff` `{reason}`
- `POST /conversations/{id}/resume`
- `POST /conversations/{id}/close`

Also: `GET /conversations/{id}/messages` currently returns tool_call/tool_result rows; add `?include_tools=false` default that filters to user/assistant/human_operator roles.

`turn.py` already skips agent when state ≠ ACTIVE_AI (ticket 04) — verify inbound persisted.

## Checks (run in order — cheapest first)
1. `uv run ruff check .`
2. `uv run alembic upgrade head`
3. `uv run pytest -q tests/test_handoff.py`
4. `uv run pytest -q`

## Done when
- Fake LLM calls `handoff_to_human` → state HUMAN_HANDOFF, handoff message sent, next user message stored, no assistant reply.
- Operator POST → message sent via adapter; `/resume` → next user message gets AI reply.

## Not included
- Operator auth beyond tenant key, assignment, SLA, notifications.
