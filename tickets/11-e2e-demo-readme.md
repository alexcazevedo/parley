---
id: 11
status: todo
model: sonnet
depends_on: [06, 08, 09, 10]
max_attempts: 3
files:
  - tests/test_e2e_demo.py
  - scripts/demo.py
  - scripts/mock_tool_server.py
  - README.md
  - docs/ARCHITECTURE.md
  - docker-compose.yml
---
## Goal
Prove the PRODUCT.md success demo end-to-end (automated + scripted) and document setup/architecture.

## Context
`tests/test_e2e_demo.py`: fake LLM scripted; Fake HTTP tool via respx; run ingest ×3 within debounce → one turn → `get_order` called with `order_id="12345"` → reply persisted; then "Quero falar com uma pessoa" → `handoff_to_human` → HUMAN_HANDOFF; next user msg → no reply; operator POST; `/resume`; next msg → reply. Asserts on DB rows + AgentRun/ToolExecution.

`scripts/mock_tool_server.py`: tiny FastAPI on :9000 with `GET /orders?order_id=` returning fake status. Add as compose service `mock-tools`. Promote `TOOL_ALLOWED_HOSTS` / `TOOL_SSRF_DISABLED` into `Settings` + `.env.example` (ssrf.py reads os.environ today; keep that working) and set `TOOL_ALLOWED_HOSTS=mock-tools` in compose env for api/worker. Add `app/config.py` and `.env.example` to your touch set.

`scripts/demo.py`: against running compose + real `LLM_API_KEY`: create tenant, agent (instructions in Portuguese, tools `[get_order]`, memory+handoff enabled), webchat channel, `get_order` tool pointing at `http://mock-tools:9000/orders`; send the 3 messages via `/dev/messages`; poll `GET /conversations/{id}/messages`; print transcript; then handoff flow; exit non-zero if expectations unmet.

README: what it is, run (`cp .env.example .env`, `docker compose up`, `uv run scripts/demo.py`), test, API overview (`/docs`), Evolution setup (webhook URL + apikey), known limitations, next 3 improvements. `docs/ARCHITECTURE.md`: modules, DB model, queue/debounce algorithm, agent loop, memory, tool model, extension points.

## Checks (run in order — cheapest first)
1. `uv run ruff check .`
2. `uv run pytest -q`
3. `docker compose up -d --build && sleep 5 && uv run scripts/demo.py`

## Done when
- `pytest` green including e2e.
- `scripts/demo.py` prints agent answer mentioning order 12345, then handoff → operator → resume flow succeeds.

## Not included
- New features. Fix bugs found; no scope additions.
