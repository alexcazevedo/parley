---
id: 13
status: done
model: sonnet
depends_on: [12]
max_attempts: 2
files: [app/static/chat.html, app/api/chat.py]
---
## Goal
Browser chat page at `GET /chat` that talks to the harness webchat channel and renders live agent replies via polling.

## Context
Ticket 12 added `app/api/chat.py` with `POST /chat/bootstrap` returning
`{api_key, channel_id, agent_id}`. Harness endpoints (all need `X-API-Key`
header): `POST /channels/webchat/messages` body
`{channel_id, user_id, message}` → `{conversation_id, ...}` (see
`app/api/webchat.py`); `GET /conversations/{id}/messages` → list of
`{role, content, ...}` (roles include `user`, `assistant`, possibly others —
render unknown roles as small gray system chips). No push transport — poll.

Build ONE self-contained file `app/static/chat.html` (inline CSS+JS, no
frameworks, no CDN), served by `GET /chat` added to `app/api/chat.py` via
`FileResponse` (path relative to module: `Path(__file__)`-based, works inside
the Docker image). Page logic:

- On load: `POST /chat/bootstrap`, keep credentials in memory; `user_id`
  from localStorage (`crypto.randomUUID()` once).
- Send box + Enter/button → POST webchat message; remember
  `conversation_id` from first response.
- Poll `GET /conversations/{id}/messages` every 1s while page open; render
  full transcript each poll (idempotent render fine at this scale): user
  bubbles right, assistant left, others centered chips.
- "Agent is typing…" indicator between send and next non-user message.
- Minimal clean styling; header "Acme — Web Chat Demo". No build step.

## Checks (run in order — cheapest first)
1. `uv run ruff check app`
2. `uv run python -c "import app.main"`
3. `docker compose up -d --build api worker && sleep 5 && curl -sf localhost:8001/chat | grep -i "<html\|chat"`
4. `curl -sf -X POST localhost:8001/chat/bootstrap | grep channel_id`

## Done when
- Open `http://localhost:8001/chat`, type a message, agent reply appears
  without refresh (given real `LLM_API_KEY` in `.env`); asking
  "meu pedido 12345" yields a reply about that order (mock get_order flow).

## Not included
- Embeddable widget, WebSocket/SSE, auth UI, theming, mobile polish, tests
- Handoff/operator UI
