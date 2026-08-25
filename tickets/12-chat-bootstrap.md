---
id: 12
status: done
model: sonnet
depends_on: []
max_attempts: 2
files: [app/api/chat.py, app/main.py, app/config.py]
---
## Goal
Idempotent `POST /chat/bootstrap` endpoint that seeds a demo tenant + get_order tool + agent + webchat channel and returns the credentials the chat page needs.

## Context
The harness (FastAPI, this repo) has no way for a browser page to obtain a
tenant API key + channel id. `scripts/demo.py` (READ IT — it is the exact
seeding recipe) creates tenant → tool → agent → channel via REST, but a fresh
tenant each run. Tenant keys are stored hashed only
(`app/models/tenant.py: api_key_hash`, helpers in `app/security/auth.py:
generate_api_key/hash_api_key`), so a repeatable bootstrap must use a FIXED
plaintext key.

Build `app/api/chat.py` with router (no auth — dev/demo shortcut, prototype):

- Setting `webchat_demo_api_key: str = "demo-webchat-key"` added to
  `app/config.py` Settings.
- `POST /chat/bootstrap`: compute `hash_api_key(settings.webchat_demo_api_key)`;
  SELECT tenant by `api_key_hash`. If absent, create (direct SQLAlchemy via
  `get_db` dep, mirror `app/api/tenants.py`) tenant "Web Chat Demo", then via
  repositories/models the `get_order` tool (same JSON as demo.py: endpoint
  `http://mock-tools:9000/orders`, method GET, timeout 5), agent "Atendente
  Acme" (instructions = demo.py `AGENT_INSTRUCTIONS`, model =
  `settings.llm_model`, tools `["get_order"]`, memory_enabled,
  handoff_enabled), webchat channel "Web Demo" wired to the agent. If tenant
  exists, look up its webchat channel. Reuse existing repositories
  (`app/repositories/`) and mirror field names from `app/api/tools.py`,
  `app/api/agents.py`, `app/api/channels.py` — do not invent schema. Commit.
- Response JSON: `{"api_key": ..., "channel_id": ..., "agent_id": ...}`.
- Register router in `app/main.py`.

## Checks (run in order — cheapest first)
1. `uv run ruff check app`
2. `uv run python -c "import app.main"`
3. `docker compose up -d --build api worker && sleep 5 && curl -sf -X POST localhost:8001/chat/bootstrap | grep channel_id && curl -sf -X POST localhost:8001/chat/bootstrap | grep channel_id`
   (second call proves idempotency — same channel_id, no crash)

## Done when
- Two consecutive bootstrap calls return identical `channel_id` and an
  `api_key` that authenticates against `GET /conversations` endpoints.

## Not included
- The chat page itself (ticket 13)
- Auth/rate-limit on bootstrap, cleanup/reset endpoint, tests
