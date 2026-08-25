---
id: 25
status: done
model: sonnet
depends_on: [24]
max_attempts: 3
files: [app/static/operator.html, app/api/operator.py, app/main.py, tests/test_operator_page.py, README.md, docs/ARCHITECTURE.md]
---
## Goal
A self-contained `/operator` page: API-key gate, handoff inbox with briefs, transcript, operator reply, Resumir/Fechar, and an Ativas tab with Assumir — driving only the existing REST API.

## Context
Read first: PRODUCT.md (operator UI), app/static/chat.html (style,
polling, `localStorage`, fetch patterns — mirror its look: same header
color, fonts, bubble classes), app/api/chat.py (`chat_page` route +
`_STATIC_DIR`), app/main.py (router registration), app/api/conversations.py
(the API: after ticket 24, `GET /conversations?state=&state=&include_handoff=
true&limit=` returns rows with `contact_external_user_id`,
`contact_display_name`, `state`, `last_message_at`, `latest_handoff:
{reason, summary, requested_by, created_at} | null`; `GET
/conversations/{id}/messages` (tool rows hidden by default; roles `user`,
`assistant`, `human_operator`, `system`); `POST /conversations/{id}/messages
{content}`; `POST /conversations/{id}/handoff {reason}`; `POST
/conversations/{id}/resume`; `POST /conversations/{id}/close`; all require
`X-API-Key`; illegal transitions return 409 with `detail`), README.md
("Run it", "API overview"), docs/ARCHITECTURE.md ("Modules" list and the
web chat mention).

Route: new `app/api/operator.py` with `router = APIRouter(prefix=
"/operator", tags=["operator"])` and `GET ""` (`include_in_schema=False`)
serving `app/static/operator.html` via `FileResponse`, same as `chat_page`.
Register in `app/main.py` right after `chat.router`.

Page (`app/static/operator.html`, plain HTML/CSS/JS, no build, no external
assets, copy in pt-BR):
- **Key gate**: on load read `localStorage["harness_operator_api_key"]`; if
  missing show a small form (input prefilled with `demo-webchat-key`,
  button "Entrar"); on submit store and start. A "Sair" link in the header
  clears it and reloads. Every fetch sends `X-API-Key`; a 401/403 clears
  the key and shows the gate again with an error line.
- **Layout**: header ("Acme — Operador"), left column = list with two tabs
  "Caixa de entrada" (`state=HUMAN_HANDOFF`) and "Ativas"
  (`state=ACTIVE_AI&state=WAITING`), right column = the selected
  conversation. Empty-state text per tab ("Nenhuma conversa aguardando").
- **List rows**: contact label (`contact_display_name` or
  `contact_external_user_id`), relative time of `last_message_at`
  ("há 2 min"), state chip; inbox rows also show `latest_handoff.
  requested_by` ("cliente" / "operador" / "sistema" for user/operator/
  system) and the `reason` truncated. Fetch with `include_handoff=true&
  limit=100`. Refresh every 3 s; keep the selected row highlighted across
  refreshes; don't rebuild the DOM if the JSON is unchanged (compare
  serialized string).
- **Conversation pane**: brief box at top (only when `latest_handoff` is
  present: requested_by, reason, summary — summary may be null → "Resumo
  indisponível"); transcript bubbles: `user` left, `assistant` right
  (muted), `human_operator` right (accent color, label "Você"), others as a
  chip like chat.html. Poll messages every 1.5 s while open; auto-scroll
  only if already at the bottom.
- **Actions** (inbox conversations): textarea + "Enviar" (POST messages;
  Enter sends, Shift+Enter newline; disabled while sending), "Resumir"
  (POST resume → conversation leaves the inbox, pane shows a notice and
  the list refreshes), "Fechar" (POST close, same). Ativas conversations:
  read-only transcript + one "Assumir" button (POST handoff with
  `{"reason": "assumido pelo operador"}` → switch to the inbox tab with
  this conversation selected). On 409 show the server `detail` inline; on
  other errors a generic line. No `alert()`/`confirm()`.
- No framework, no external fonts/CDN; keep it under ~450 lines.

Tests `tests/test_operator_page.py`: (a) `GET /operator` → 200,
`text/html`, body contains `harness_operator_api_key`, `Caixa de entrada`,
`Assumir`; (b) `/operator` needs no API key (it is the gate itself) while
`GET /conversations` without a key is 401/403 (whatever the existing deps
return — assert the status the app already uses).

Docs: README "API overview" table gets a `GET /operator` row and the "Run
it" block a line "operator page: http://localhost:8001/operator (paste the
tenant API key; the demo key is prefilled)"; docs/ARCHITECTURE.md "Modules"
gets `app/api/operator.py` + `app/static/operator.html` with one sentence
each and a short "## Operator UI" subsection (key gate, inbox/Ativas,
polling, endpoints used, what it is not: no accounts).

## Checks (run in order — cheapest first)
1. `uv run ruff check app tests`
2. `uv run python -c "import app.main"`
3. `docker compose up -d postgres && uv run pytest tests/test_operator_page.py -q`
4. `uv run pytest -q`
5. Live demo (stack rebuilt: `docker compose up -d --build api worker && sleep 6`; real LLM key in .env): script over HTTP — bootstrap `POST /chat/bootstrap`; send "Quero falar com uma pessoa" via `/channels/webchat/messages` (X-API-Key demo-webchat-key); wait ~20 s; `GET /conversations?state=HUMAN_HANDOFF&include_handoff=true` must list the conversation with a non-null `latest_handoff`; `POST /conversations/{id}/messages {"content":"Oi, aqui é a Ana"}` → 201 and the message shows in `GET …/messages` with role `human_operator`; `POST …/resume` → 200 state ACTIVE_AI; then send "obrigado, e o pedido 12345?" and after ~20 s an `assistant` message exists after the operator's. Then open http://localhost:8001/operator in a browser (Chrome tools if available, else `curl -s localhost:8001/operator | grep -c "Caixa de entrada"`) and confirm the page renders the inbox row and brief — describe what was seen in the report; do not claim the visual check if it was not done.

## Done when
- PRODUCT.md success demo passes end-to-end (handoff from `/chat` →
  appears in `/operator` with brief → reply lands in `/chat` → Resumir →
  AI answers again; Assumir moves an active conversation into the inbox);
  suite green; docs updated.

## Not included
- Accounts/login, assignment, notifications, unread counters, SSE, admin
  CRUD screens, run/tool/sentiment views, mobile layout polish.
