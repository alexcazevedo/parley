---
id: 07
status: doing
model: sonnet
depends_on: [06]
max_attempts: 3
files:
  - app/memory/__init__.py
  - app/memory/service.py
  - app/repositories/memories.py
  - app/tools/builtin.py
  - app/tools/registry.py
  - app/api/contacts.py
  - app/conversations/summary.py
  - app/conversations/turn.py
  - tests/test_memory.py
  - tests/test_summary.py
---
## Goal
Long-term memory (store/search/CRUD + `remember` built-in tool) and rolling conversation summary, both fed into context.

## Context
`MemoryService(session)`: `store(tenant_id, contact_id, type, content, importance, expires_at?, metadata?)`, `get_contact_memories(tenant_id, contact_id, limit=20)` ordered importance desc, updated_at desc, excluding expired; bump `last_accessed_at`; `search(tenant_id, contact_id, query)` = ILIKE for now (docstring: swap for vector later); `forget(id)`. REST `/contacts/{id}/memories` GET/POST/DELETE added to existing `api/contacts.py` router (do NOT touch `app/main.py` — parallel ticket owns it).

`tools/builtin.py::BuiltinExecutor` handles `remember(type, content, importance)` (only if `agent.memory_enabled`); tool spec injected to LLM tools list by registry. `handoff_to_human` added in ticket 08 — leave a registration hook.

`summary.py::maybe_summarize(agent, conversation, llm)`: when unsummarized message count since `conversation.summary_cursor` (add nullable column via migration `0003`) ≥ `agent.summary_threshold`, call LLM with prior summary + those messages → replace `conversation.summary`, advance cursor. Raw messages untouched. Run after turn, in try/except (never fails turn).

`turn.py`: pass `memories` to ContextBuilder; call `maybe_summarize` after reply.

## Checks (run in order — cheapest first)
1. `uv run ruff check .`
2. `uv run alembic upgrade head`
3. `uv run pytest -q tests/test_memory.py tests/test_summary.py`
4. `uv run pytest -q`

## Done when
- Fake LLM calls `remember` → Memory row; next turn system prompt contains it; tenant B cannot read it.
- After `summary_threshold` messages, `conversation.summary` set, message count unchanged.

## Not included
- Vector search, auto-extraction, handoff.
