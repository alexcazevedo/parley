# Conversational Agent Harness

Headless, multi-tenant infrastructure for AI-powered conversational apps. The
harness owns the operational complexity of a conversation — ingestion, queue,
per-conversation debounce, conversation state, short/long-term memory, LLM
execution, tool orchestration, human handoff, retries, idempotency,
observability — so a customer only implements business logic as HTTP tools
and points a channel (WhatsApp via Evolution API, or their own Web Chat
widget) at the harness.

Warm handoff: when a conversation escalates to a human, the harness writes a
best-effort AI-summarized operator brief and surfaces it as `latest_handoff`
on `GET /conversations/{id}`, so the operator doesn't make the contact repeat
themselves.

Sentiment escalation: on agents with `sentiment_escalation_enabled`, a cheap
classifier scores each inbound batch for frustration before the agent runs, and
a score at or above `SENTIMENT_THRESHOLD` (0.7) skips the AI answer entirely —
the conversation warm-hands off with `requested_by=system` and the user gets
the agent's `handoff_message`.

**Principle**: the customer implements the business tools; the harness
manages the conversation and agent runtime.

Modular monolith: Python 3.12, FastAPI, PostgreSQL (SQLAlchemy 2 + Alembic),
Redis (queue + locks + debounce), Docker Compose (`api`, `worker`, `postgres`,
`redis`, plus a `mock-tools` service for the demo).

See `PRODUCT.md` for the full product framing and `docs/ARCHITECTURE.md` for
a module-by-module tour, the DB model, the debounce algorithm, the agent
loop, memory, the tool model and extension points.

## Architecture (at a glance)

```
                 ┌──────────────┐        ┌───────────────┐
 Web Chat widget │              │        │               │
 (or /dev/msgs)  │  api (FastAPI) ──────▶│  PostgreSQL   │
        ▼        │              │        │  (messages,   │
 POST /channels/  │  - ingest()  │        │   conversations,
  webchat/messages│  - REST CRUD │        │   agent_runs,  │
        │         │  - webhooks  │        │   tools, ...)  │
        ▼         └──────┬───────┘        └───────▲───────┘
   ┌─────────┐           │ enqueue Job             │
   │ Evolution│           ▼                         │
   │  (WA)   │      ┌──────────┐                   │
   └────┬────┘      │  Redis   │                   │
        │            │ jobs +   │                   │
        ▼            │ debounce │                   │
 POST /webhooks/     │ + locks  │                   │
  whatsapp/{id}      └────┬─────┘                   │
                           ▼                         │
                     ┌───────────┐   AgentRuntime    │
                     │  worker   │───────────────────┘
                     │           │   (context, LLM,
                     │ job loop +│    tools, memory,
                     │ scheduler │    handoff)
                     └─────┬─────┘
                           │
                ┌──────────┼───────────┐
                ▼          ▼            ▼
          LLM provider  customer's   built-ins
          (OpenAI-      HTTP tools   (remember,
           compatible)  (SSRF-guarded) handoff_to_human)
```

Inbound messages (Web Chat REST, the `/dev/messages` simulator, or the
WhatsApp/Evolution webhook) all funnel through the same `ingest()` — nothing
bypasses the pipeline. A burst of messages on one conversation collapses into
a single agent turn once the debounce window closes (see
`docs/ARCHITECTURE.md`), and the turn's reply goes back out through the same
`ChannelAdapter` the message came in on.

**Humanizer**: replies feel human — not bot-instant. Enabled per channel via
`config: {"humanize": true}`, the humanizer marks inbound messages as read
(WhatsApp), sends "composing…" presence (WhatsApp), splits long replies into
2–4 short bubbles, and paces each bubble's send with a delay proportional to
its length (typing speed, min/max delay per bubble, and split count are
configurable global Settings). Web Chat keeps legacy single-message delivery
when humanize is off; all channels support split, paced bubbles when on. See
`docs/ARCHITECTURE.md` for the full flow and adapter hooks.

**Barge-in**: a message arriving mid-reply supersedes the turn at safe
checkpoints; the conversation re-runs with the full batch.

## Run it

```bash
cp .env.example .env
# then edit .env. Before first boot you MUST set real values for
# ADMIN_API_KEY and SECRETS_KEY (a Fernet key; generate one with
# `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`).
# The api and worker refuse to start while either still has the shipped
# placeholder, unless DEMO_MODE=true. With DEMO_MODE=true the placeholder
# SECRETS_KEY lets the app boot, but it cannot encrypt: creating a channel
# with a `secret` or a tool with `auth_config` then returns 500. Demo users
# who need those features must set a real SECRETS_KEY (same command as above).
#
# For the local demo set DEMO_MODE=true as well. It mounts /chat,
# POST /chat/bootstrap and POST /dev/messages, which are what /chat,
# scripts/demo.py and scripts/demo_media.py use; it is off by default.
# Also set a real LLM_API_KEY (OpenAI or any OpenAI-compatible
# endpoint — OpenRouter, Ollama, vLLM, ...) for the live demo. Everything
# else in .env.example has a working default for local Docker Compose.

docker compose up -d --build
# starts postgres, redis, api (:8001 on the host, runs migrations on boot),
# worker, and mock-tools (:9000 inside the compose network — the demo's
# fake business endpoint).

# operator page: http://localhost:8001/operator (paste the tenant API key;
# the key is prefilled only in demo mode — otherwise you paste it yourself)

# web chat page (DEMO_MODE=true only): http://localhost:8001/chat — besides typing, the composer
# has a 📎 button (attach a photo or audio file) and a 🎤 button (record a
# voice note in the browser via MediaRecorder, click again to send it).
# Both pages (/chat and /operator) render image messages as thumbnails and
# audio messages as a player plus the transcript text underneath.

uv run scripts/demo.py
# scripts the PRODUCT.md "success demo" against the running stack over
# plain HTTP: creates a tenant/agent/tool/channel, sends "Oi" / "Queria
# saber onde está" / "meu pedido 12345" in quick succession, prints the
# transcript once the agent replies (expects it to mention order 12345,
# having called the mocked get_order tool), then "Quero falar com uma
# pessoa" -> handoff -> operator reply -> /resume -> one more message.
#
# Exits 0 on success, 2 if LLM_API_KEY in .env is still the placeholder
# (the pipeline itself is still proven by `pytest`'s scripted-LLM e2e test,
# no live key needed for that), 1 if an expectation wasn't met.

uv run scripts/demo_media.py
# scripts the PRODUCT.md media success demo: bootstraps the same /chat demo
# tenant, uploads tests/fixtures/order_label.png (shows "PEDIDO 12345") with
# a caption, then tests/fixtures/voice_note.mp3 (a real voice note asking
# about the order) within the debounce window so both land in one turn,
# waits for the agent's reply, and asserts it mentions the order after
# reading the image and transcribing the audio. Same exit codes as demo.py.
```

Host ports: Postgres `5434`, Redis `6380`, API `8001` (overridable via
`POSTGRES_PORT`/`REDIS_PORT`/`API_PORT` in `.env` — chosen to avoid clashing
with other local services on the usual `5432`/`6379`/`8000`). Inside the
Compose network, services address each other by service name
(`postgres:5432`, `redis:6379`, `mock-tools:9000`) regardless of the
host-side ports.

## Test

```bash
uv run ruff check .
uv run pytest -q
```

Tests run against a real Postgres (`harness_test`, auto-created if missing —
see `tests/conftest.py`) on the host-mapped port and `fakeredis`; the LLM is
always `FakeLLMProvider` (scripted, in-process) and any customer HTTP tool
call goes through `respx`, so `pytest` never makes a network call and never
needs a real `LLM_API_KEY`. The network is blocked in the tests: an autouse
guard in `tests/conftest.py` refuses any connection other than to loopback or
a unix socket (and any lookup of another host than `localhost`, `*.example.com`
and a few test names), and a test that tried fails at teardown listing the
hosts. A test that makes a handoff uses the `fake_handoff_brief` fixture for
the operator brief. `tests/test_e2e_demo.py` is the automated
equivalent of `scripts/demo.py` — the full success-demo flow (debounced
turn, tool call, handoff, operator, resume) over the real pipeline.

## API overview

Full interactive docs at `GET /docs` (Swagger UI) once the API is running.
Auth: `X-Admin-Key` (one shared admin key, `/tenants` only) or `X-API-Key`
(per-tenant, everything else). Highlights:

| Route | Purpose |
|---|---|
| `POST /tenants` | admin-only; returns the tenant's `api_key` (shown once) |
| `POST /agents`, `/channels`, `/tools` | configure an agent, a channel, a business tool. Ids in the body (`agent_id`, `channel_id`) must belong to the caller's tenant, otherwise 422 (`agent not found` / `channel not found`). On create and update a tool `endpoint` is rejected with 422 `invalid endpoint: ...` when its host is `localhost`, `*.localhost`, `*.local`, `*.internal`, or an IP literal in any IPv4 notation (`127.1`, `0x7f000001`, ...) or IPv6 that is not globally routable (e.g. `http://100.64.0.1/x`). Other hostnames pass at create; DNS resolution and the check of every returned address happen only at call time, where a host resolving to a private IP is refused. Tool requests send `Accept-Encoding: identity`; a response with any other `Content-Encoding` is a failed result (`unsupported response encoding`) |
| `POST /channels/webchat/messages` | Web Chat ingestion for server-side callers holding a tenant key. Per-user then per-tenant rate limits apply (fixed one-minute windows; a user already over their own limit does not count toward the tenant limit): over the limit -> 429 |
| `POST /channels/webchat/media` | Web Chat media upload (multipart: `channel_id`, `user_id`, `file`, optional `caption`/`message_id`) — a photo or voice note, same pipeline as text. No `Content-Length` is required. The cap counts bytes actually received: a body over `MEDIA_MAX_BYTES` + 1 MiB -> 413 `request body too large`, a file over `MEDIA_MAX_BYTES` -> 413 `media exceeds max size`; the media type is decided from the file content, not the client header (unknown content -> 415) |
| `GET /media/{message_id}` | tenant-scoped media download (`X-API-Key` required) — serves the file behind an image/audio message, used by `/chat` and `/operator` to render thumbnails/players |
| `POST /chat/bootstrap`, `GET /chat` | **demo-only** (`DEMO_MODE=true`; 404 otherwise): unauthenticated tenant bootstrap and the built-in chat page. Outside demo mode the demo tenant key (`WEBCHAT_DEMO_API_KEY`) is refused with 401 like any unknown key |
| `POST /dev/messages` | **demo-only** (`DEMO_MODE=true`; 404 otherwise): dev simulator — same pipeline as Web Chat, for scripting demos |
| `POST /webhooks/whatsapp/{channel_id}` | Evolution API inbound webhook. Tenant and channel come from the authenticated channel, never from the payload. When the user or tenant rate limit is exceeded it still answers 200 `{"status": "rate_limited"}`: the message is stored (media as `[mídia não recebida]`) and visible to operators, but no agent turn runs, and a warning is logged. An Evolution media download over the size limit is cut off and stored the same way (`unsupported`, `[mídia não recebida]`) |
| `GET /conversations/{id}/messages` | poll for replies (`?include_tools=false` by default; `?after=<timestamp>`, compared with `created_at`). Messages come in the order they were stored (`seq`) |
| `POST /conversations/{id}/messages` | operator reply (typically while `HUMAN_HANDOFF`) |
| `POST /conversations/{id}/handoff` / `/resume` / `/close` | conversation state transitions. An operator's action is saved at once and stops a running reply: a turn checks the state before each send, sends nothing more (bubbles already sent stay) and the customer is not told about the cut-off. A tool call already in flight still completes |
| `GET/POST /categories`, `GET/PATCH/DELETE /categories/{id}` | the tenant's categories (`name`, `description`) |
| `PUT /conversations/{id}/category` | a human sets (or, with `null`, clears) the category; the model no longer changes it |
| `GET /lines` | the waiting lines: one per category, then the uncategorized one |
| `POST /conversations/{id}/claim` / `/release` | an operator takes a waiting conversation, or puts it back |
| `GET/POST/DELETE /contacts/{id}/memories` | long-term memory CRUD |
| `GET /operator` | self-contained operator inbox page (handoff triage, reply, resume/close) |

### Errors

Every error uses one envelope: `{"error": {"code", "message", "request_id"}}`.
The webchat, media and dev routes (`POST /channels/webchat/messages`,
`POST /channels/webchat/media`, `POST /dev/messages`) given a channel of
another type answer 422 `{"error":{"code":"http_error","message":"channel is
not a webchat channel","request_id":"..."}}`; a channel of another tenant is
still 404. A request that fails validation answers 422 with `details`, one
entry per problem; `field` is the location without the leading
`body`/`query`/`path`, and the input values are never echoed:

```json
{"error": {"code": "validation_error", "message": "Validation failed",
           "request_id": "9f0c...", "details": [{"field": "name", "message": "Field required"}]}}
```

### What the model sees of a conversation

`agent.context_message_limit` (default 20 when unset or 0) counts customer
messages, operator messages and assistant messages that are not tool calls.
The history sent to the model starts at the limit-th most recent of those and
carries every row after it, tool calls and tool results included, so a call is
never separated from its result. A tool-heavy agent therefore sends a longer
prompt than the limit suggests; there is no separate cap on its size.

## Evolution API (WhatsApp) setup

1. Create a channel: `POST /channels` with `type: "whatsapp_evolution"`, `config:
   {"base_url": "...", "instance": "..."}` and `secret` set to the Evolution
   instance's `apikey` (encrypted at rest, never returned by the API).
2. In Evolution, point that instance's webhook at:
   `POST https://<your-harness-host>/webhooks/whatsapp/{channel_id}`
3. Evolution must send the shared secret on every webhook call as the
   `apikey` header — the harness compares it (constant-time) against the
   channel's stored secret and returns `401` on a mismatch.
4. Outbound replies go back out through Evolution's `sendText` REST endpoint
   using that same `apikey`, `base_url` and `instance`.

Who is answered: only people. Senders at `@s.whatsapp.net` and bare ids are
answered under their phone number; senders at a WhatsApp privacy id (`@lid`)
are answered too and keep the full `…@lid` as their contact id. Groups
(`@g.us`), status broadcasts (`status@broadcast`), newsletters and any other
suffix are ignored: the webhook answers `{"status": "ignored"}`, and nothing
is stored or sent. Limits: replies to an `@lid` are sent to that id, and
whether Evolution delivers them has not been checked; test it once against a
real instance. A person seen under both an `@lid` and a phone number is two
contacts.
5. `base_url` must be a public address. On create and update, a channel of
   type `whatsapp_evolution` whose `config.base_url` is not globally routable
   gets 422 `invalid base_url: ...`. The address is checked again on every
   outbound call (redirects are not followed). A refused call is logged as
   `channel.endpoint_refused`; an operator reply through such a channel
   returns 502 `channel endpoint refused`, and a media download that is
   refused is stored as an `unsupported` message.
6. To use a private host (for example an Evolution container in the same
   Compose network), the operator lists it in `CHANNEL_ALLOWED_HOSTS`, a
   comma-separated list of hostnames, in `.env` for both `api` and `worker`:

   ```
   CHANNEL_ALLOWED_HOSTS=evolution
   ```

   With that set, `base_url` may be `http://evolution:8080`. The list is
   set by the operator only; tenants cannot change it. It is additive: a
   listed host skips the address check and every other `base_url` must still
   be public. It is separate from `TOOL_ALLOWED_HOSTS`: a host listed for
   tools is not thereby allowed for channels, and the other way round.
7. The webhook answers before any media is downloaded. It authenticates,
   parses, recognises a repeated delivery (answers `{"status": "ok"}` and does
   nothing more: no download, no second message), applies the rate limits and
   stores the message; an image or audio message is stored with its type and
   no file yet. The worker downloads the file in the turn that needs it. The
   stored copy of the event has `base64` and `jpegThumbnail` removed. Events
   stored before this change still contain them.
8. Media downloaded from Evolution is read up to a cap derived from
   `MEDIA_MAX_BYTES`. A longer download, a compressed response or decoded
   media over `MEDIA_MAX_BYTES` is cut off and stored as an `unsupported`
   message with the content `[mídia não recebida]`. The whole download must
   finish within 30 seconds (request and body); a slower one is stored the
   same way. A server that escapes `/` as `\/` in its base64 makes the
   response larger, so media close to the limit can be refused.

### After upgrading

Existing channels with a private `base_url` (for example
`http://10.0.0.5:8080` or `http://evolution:8080`) stop sending after the
upgrade until the operator lists the host in `CHANNEL_ALLOWED_HOSTS`. Older
versions also allowed a channel or conversation to point at another tenant's
agent; such a conversation now gets no turn and no summary.

To list the affected rows, run once after upgrading:

```bash
docker compose run --rm api python -m app.audit
# or, outside Compose: python -m app.audit
```

This plain command checks `base_url` addresses without DNS, so a private
hostname such as `http://evolution:8080` is not listed by it. To list those
too, add `--resolve`, which looks each hostname up and lists channels that
resolve to a private address or do not resolve:

```bash
docker compose run --rm api python -m app.audit --resolve
# or, outside Compose: python -m app.audit --resolve
```

Run `--resolve` where the server runs, so it uses the same DNS.

It prints one line per finding, `<kind> <row_id> tenant=<tenant_id> <detail>`,
then a last line `N offending rows`. The exit code is 1 when N is above 0,
otherwise 0. The kinds:

- `channel_foreign_agent`: a channel whose `agent_id` belongs to another
  tenant (`detail` is `agent=<id>`).
- `conversation_foreign_agent`: a conversation whose `agent_id` belongs to
  another tenant (same `detail`).
- `channel_refused_base_url`: an Evolution channel whose `base_url` the check
  refuses (`detail` is the reason). Listing the host in
  `CHANNEL_ALLOWED_HOSTS` removes it from the report.

The command only reads. It changes nothing and prints no secrets. Without
`--resolve` it makes no network or DNS call. Fixing the rows is up to the operator.

## Agent boundaries

An agent can carry its own limits next to `instructions`: what it is for
(`boundary_scope`), what it must never say (`boundary_never`) and the text
sent instead of a blocked reply (`boundary_refusal_message`). Set them with
`POST /agents` or `PATCH /agents/{id}`. All four fields are optional:

| Field | Limits | Default |
|---|---|---|
| `boundary_scope` | text, up to 2000 characters | none |
| `boundary_never` | list, up to 50 items, each 1-300 characters (trimmed) | `[]` |
| `boundary_refusal_message` | text, 1-1000 characters | `Desculpe, não posso ajudar com esse assunto.` |
| `boundary_check_enabled` | `true` or `false` | `true` |

Example (placeholder values; replace `$TENANT_API_KEY` and the model id):

```bash
curl -X POST http://localhost:8001/agents \
  -H "X-API-Key: $TENANT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Recepção da clínica",
    "instructions": "Você atende a recepção de uma clínica.",
    "model": "your-model-id",
    "boundary_scope": "agendamento de consultas, horários e endereço da clínica",
    "boundary_never": [
      "orientação médica ou diagnóstico",
      "promessas de preço ou desconto",
      "comentários sobre concorrentes"
    ],
    "boundary_refusal_message": "Desculpe, só consigo ajudar com agendamentos e informações da clínica."
  }'
```

How it is enforced:

- The boundaries are written into the system prompt under a `## Boundaries`
  heading, in fixed wording, together with a rule to stay in role and not
  reveal the instructions.
- With the check on, each model-written reply is judged once before it is
  sent. An allowed reply goes out unchanged. A blocked reply is replaced by
  the agent's refusal message. The run row keeps `boundary_reason` and
  `blocked_text` (the original reply, never sent), and a
  `boundary.reply_blocked` warning is logged.
- The check fails closed. An error, a timeout (`BOUNDARY_CHECK_TIMEOUT_SECONDS`,
  default `8`) or an answer that is neither `ALLOW` nor `BLOCK: <reason>`
  blocks the reply. The recorded reason is `check_failed` or
  `check_unparseable`.
- Fixed fallback messages and the handoff text are not checked, only replies
  the model wrote.

**Cost:** with the check on, every model-written reply costs one extra model
call. This is counted in the run's `llm_calls`. Set `boundary_check_enabled`
to `false` to skip it. That is ignored while an operator floor is set (see
below). An agent with no boundaries and no floor makes no extra call and
behaves as before.

### Operator floor

The operator can set a floor that applies to every agent of every tenant.
Point `BOUNDARY_FLOOR_FILE` at a JSON file, for example:

```json
{
  "scope": "atendimento ao cliente da loja",
  "never": ["conteúdo ofensivo", "dados de outros clientes"]
}
```

- Keys: `scope` (optional, non-empty string) and `never` (optional list of
  non-empty strings). Any other key is an error.
- Tenants can only add to the floor. The floor scope and never-items are
  always kept, and while a floor is set the reply check cannot be switched
  off.
- A missing or malformed file stops both the `api` and the `worker` at
  startup (`BoundaryConfigError`). Both processes must be able to read the
  file. Edits to the file take effect after a restart.
- Unset or empty `BOUNDARY_FLOOR_FILE` means no floor.

### Not included

- The customer's message is not screened before the agent runs.
- A blocked reply is refused at once; the agent is not asked again.
- No hand-off to a human after repeated blocks.
- No boundaries shared across all agents of one tenant.
- Replies written by human operators are not checked.
- No endpoint or page shows blocked replies. Read `agent_runs`
  (`boundary_reason`, `blocked_text`) directly, and the `boundary.*` logs.
- Block and false-block rates on a real model are not measured yet. The tests
  use a scripted model.

## Categories and waiting lines

Every conversation can have one category, chosen by the model from a list the
tenant defines. A conversation handed to a human waits in a line, one line per
category. Operators claim from a line.

**What the harness owns:** the category of a conversation, since when it
waits (`waiting_since`), and who claimed it (`claimed_by`, `claimed_at`),
including the race: two operators cannot hold the same conversation.

**What your app owns:** operator accounts, which operator may see which line,
priorities and timers, notifications, and real operator screens. The
`operator_id` is any text your app sends (1 to 200 characters). The harness
stores it and does not check it. `/operator` is a demo page, not a product
screen.

How it works:

- A tenant defines categories (`name`, unique per tenant, and a short
  `description` the model reads). They are shared by all its agents and
  channels. With no categories the feature is off: no classifier call is made
  and handed-off conversations wait in the uncategorized line.
- On every batch of customer messages, and also on batches that arrive while a
  human has the conversation (`HUMAN_HANDOFF`), one extra model call picks a
  category from the list, or none. The model never creates a category and never
  clears one: when nothing fits the conversation stays where it was. A failed or
  slow call (`CATEGORY_TIMEOUT_SECONDS`, default `5.0`) changes nothing and the
  reply goes out as usual.
- `PUT /conversations/{id}/category` sets the category in any state and locks
  it: the model no longer changes that conversation. `{"category_id": null}`
  clears it and locks it too. There is no unlock.
- Deleting a category leaves its conversations uncategorized. They keep their
  wait position.
- A handoff sets `waiting_since`. `GET /lines` lists, per category by name and
  then the uncategorized line, the `HUMAN_HANDOFF` conversations nobody has
  claimed, oldest `waiting_since` first. A move to another category keeps
  `waiting_since`, so the conversation joins the new line at its original place.
  `?limit=` (1 to 500, default 200) caps the total, keeping the oldest;
  `?include_handoff=true` adds the handoff brief.
- `claim` is refused with 409 `conversation is already claimed` when another
  operator holds it, and with 409 `conversation is not waiting for an operator`
  when it is not in `HUMAN_HANDOFF`. A claimed conversation leaves the line.
  `release` puts it back at its original place (409 `conversation is not
  claimed` if nobody holds it). `resume` and `close` clear the claim and
  `waiting_since`.
- List filters on `GET /conversations`: `category_id=<uuid>`,
  `uncategorized=true` (not together with `category_id`: 422), `claimed_by=<id>`.
  They combine with `state`.

Walk-through (`$BASE` is the API, `$KEY` the tenant key; `$CHANNEL` a webchat
channel id):

```bash
H="X-API-Key: $KEY"
# 1. Define two categories
curl -s -X POST $BASE/categories -H "$H" -H 'Content-Type: application/json' \
  -d '{"name":"billing","description":"invoices and payments"}'
curl -s -X POST $BASE/categories -H "$H" -H 'Content-Type: application/json' \
  -d '{"name":"support","description":"help with the product"}'
# 2. A customer writes; the worker files the conversation (wait for the reply)
curl -s -X POST $BASE/channels/webchat/messages -H "$H" -H 'Content-Type: application/json' \
  -d "{\"channel_id\":\"$CHANNEL\",\"user_id\":\"ana\",\"message\":\"preciso da segunda via do boleto\"}"
curl -s "$BASE/conversations?category_id=$BILLING_ID" -H "$H"
# 3. Hand it to a human (or let the agent do it) and look at the lines
curl -s -X POST $BASE/conversations/$CONV/handoff -H "$H" -H 'Content-Type: application/json' -d '{}'
curl -s $BASE/lines -H "$H"
# 4. Move it to another line (it keeps its place)
curl -s -X PUT $BASE/conversations/$CONV/category -H "$H" -H 'Content-Type: application/json' \
  -d "{\"category_id\":\"$SUPPORT_ID\"}"
# 5. Claim it; a second operator gets 409
curl -s -X POST $BASE/conversations/$CONV/claim -H "$H" -H 'Content-Type: application/json' \
  -d '{"operator_id":"op-1"}'
curl -s -X POST $BASE/conversations/$CONV/claim -H "$H" -H 'Content-Type: application/json' \
  -d '{"operator_id":"op-2"}'
# 6. Release it: back in the line at its original place
curl -s -X POST $BASE/conversations/$CONV/release -H "$H"
```

`/operator` has the tabs "Filas" (the lines, with the wait time, a button
"Atender" per row and a category selector), "Minhas" (claimed by the id in the
"Seu nome" field) and "Ativas". Migrations `0014` (categories,
`conversations.category_id`, `category_locked`) and `0015` (`waiting_since`,
`claimed_by`, `claimed_at`, a partial index on waiting conversations) run on
boot with the others. Limits are in `docs/FOLLOW_UPS.md`, "From the categories
run".

## Reliability

What happens to a customer's message when a worker dies or a turn fails.
The tests are in `tests/test_e3_reliability.py` (one per case below) and in
the per-feature files `tests/test_queue_recovery.py`,
`tests/test_claim_recovery.py`, `tests/test_lock_heartbeat.py`,
`tests/test_turn_retry.py` and `tests/test_scheduler_concurrency.py`. The
tool ledger, the short retry and the held-transaction rules are covered in
`tests/test_e3_part2.py`.

**Delivery is at-least-once, not exactly-once.** A worker that dies after it
sent a reply and before it marked the turn done repeats that reply after
recovery (about 60 s later). A send that raised an error but did reach the
customer is retried and can repeat. See "Limits" below.

### A worker crashes

- **Before the message is in the debounce batch.** A worker moves a job from
  `harness:jobs` to its own list `harness:processing:{worker_id}` in one step
  and removes it only after the message is in the batch. Every worker
  refreshes an alive key (`harness:worker:{id}`, 30 s TTL) and runs a
  housekeeping pass every 10 s. A pass puts the jobs of any worker whose alive
  key has expired back on `harness:jobs` (log event `worker.jobs_reaped`).
  A message that reaches the batch twice is answered once.
- **After a conversation was claimed.** The scheduler does not remove a due
  conversation. It takes a claim, the key `harness:claim:{conversation_id}`
  (set only if absent, 60 s TTL), and moves the due score 60 s out
  (`CLAIM_TIMEOUT_SECONDS`); only the worker that gets the key runs the turn.
  The messages
  being answered wait in `harness:inflight:{conversation_id}` until the turn
  is finished. If the worker dies in between, the conversation is due again
  after 60 s and the next turn answers the same messages.
- **During a long turn.** The conversation lock (`harness:lock:{id}`, 60 s) is
  renewed every 20 s while the turn runs, and so is the claim. A turn longer
  than the lock's lifetime is never run twice in parallel. A turn that finds
  the lock is no longer its own stops before it sends or saves anything else,
  and leaves its messages to the current holder.

### A turn fails

Anything that escapes the turn (a send that fails, a database error, a
speech-to-text failure) puts the messages back for another attempt:

- Up to 3 attempts (`MAX_TURN_ATTEMPTS`), 2 s after the first failure and 8 s
  after the second (`RETRY_BACKOFF_SECONDS`). The wait is counted from the
  moment the turn failed, not from when it started. Messages that arrive in the
  meantime join the next attempt. Log event `scheduler.turn_retry`.
- After the third failure the batch goes to the dead-letter list
  `harness:dlq:turns`, and the customer gets one error message
  (`TURN_FAILED_MESSAGE`). Log event `scheduler.turn_dead_lettered`.
- If part of a reply was already delivered (the first bubble went out, the
  second failed), there is no retry: what was sent stays, the rest is dropped,
  the batch is dead-lettered with `partly_delivered` true and no error message
  is added.

Read the dead-letter list with Redis:

```bash
docker compose exec redis redis-cli LRANGE harness:dlq:turns 0 -1
```

Each entry is a JSON object: `conversation_id`, `message_ids`, `error`
(`"Type: message"`), `partly_delivered`, `attempts` and `failed_at` (unix
seconds). Oldest first. Nothing reads the list or removes entries: there is no
API or page for it and no replay (see `docs/FOLLOW_UPS.md`). `harness:dlq`
is a different list, the job queue's own (`RedisQueue.fail`); the worker does
not call it today.

### Tool calls are recorded first and not repeated

Before a tool runs, its call (tool name and arguments) is saved in its own
transaction with status `started` in `tool_executions`; when the tool returns,
the row is completed with the result (or the error). The record therefore
stays when the turn later fails, is cancelled or is superseded. When a turn
runs again for the same messages (a retry, or a turn merged with newer
messages), a call with the same tool and the same arguments as an earlier
attempt is not made again:

- **Reuse.** If the earlier call returned, the model gets the saved result,
  an error included, and the tool is not called. This applies to every tool.
  It holds only within re-runs of the same turn, never in a later turn with
  other messages. Log event `tools.result_reused`.
- **Unknown outcome.** If the earlier call was started and has no result, an
  HTTP tool with method GET is called again. That covers a worker that died
  mid-call and an HTTP tool call that failed after its request may have
  reached the endpoint: a timeout other than a connect timeout, another
  transport error, or an unexpected error (the row stays `started`, with the
  error and latency filled in). A call that never left (connection refused,
  connect timeout, blocked endpoint, invalid arguments) or that got an HTTP
  response, of any status, has a saved result. Any other HTTP tool is not: the model is told the outcome is unknown
  and that the call was not repeated, and the call is logged as
  `tools.outcome_unknown` (warning). The built-in tools (`remember`,
  `handoff_to_human`) save their effect together with their result, so a call
  with no result did nothing and runs again. A reused `handoff_to_human` result
  still ends the turn with the agent's handoff message.
- **Same turn.** Another run in the same conversation whose saved message ids
  are all among this run's message ids.

### Short retry on 429/5xx

Model calls, speech-to-text and GET tools are retried when the answer is 429,
500, 502, 503 or 504: up to `HTTP_RETRY_ATTEMPTS` tries in total, waiting a
growing time between them (log event `http.retry`). A numeric `Retry-After`
header sets the wait, up to `HTTP_RETRY_MAX_WAIT_SECONDS`. Not covered:
non-GET tools and sends to the channel are never retried at this level, and
nothing is retried after a timeout or a connection error, because the request
may have been processed. A model call that still fails after the last try
gets the runtime's own error text.

| Setting | Default | Meaning |
| --- | --- | --- |
| `HTTP_RETRY_ATTEMPTS` | `3` | Tries in total, the first included |
| `HTTP_RETRY_BASE_SECONDS` | `0.5` | Wait after the first failure; doubles each time |
| `HTTP_RETRY_MAX_WAIT_SECONDS` | `10` | Longest single wait, `Retry-After` included |
| `LLM_MAX_TOKENS_PARAM` | `max_tokens` | Name of the token-limit field sent to the model; some models want `max_completion_tokens` |

The API and the worker each keep one shared HTTP client for the model,
speech-to-text, the channel and HTTP tools, and close it on shutdown.

### Redis errors in the worker

An error in the job loop (Redis down or restarting, a job that cannot be
handled) is logged as `worker.job_loop_error`; the loop waits 1 s
(`JOB_LOOP_ERROR_PAUSE_SECONDS`) and tries again. The worker does not exit.
The housekeeping pass and the scheduler loop also log their errors and go on.

### Concurrency

One worker runs up to `WORKER_CONCURRENCY` conversations at the same time
(default 4, minimum 1). One conversation never runs twice at once. A turn
holds a database connection only while it reads or writes: it commits before
every model call, tool call, speech-to-text call, send to the channel and
paced wait, so the connection goes back to the pool during that slow work.
The one exception is the API's request paths, which still hold a transaction
while they fetch media (see "Limits"). The app's engine uses SQLAlchemy's
default pool (5 connections, up to 10 more on demand), so keep
`WORKER_CONCURRENCY` well under that.

### Graceful shutdown

On `SIGTERM` or `SIGINT` the worker takes no new job and no new turn, and
waits up to 90 s (`SHUTDOWN_GRACE_SECONDS`) for the running turns to finish.
After that it cancels what is left: those turns release their lock, their
uncommitted work is rolled back, and their messages come back after the claim
timeout. The shipped `docker-compose.yml` sets `stop_grace_period: 100s` on
the `worker` service (Docker's default is 10 s) and `restart: unless-stopped`.
Log events: `worker.stopping`, `scheduler.stopping`, `scheduler.stopped`,
`worker.shutdown_grace_expired`, `scheduler.turns_cancelled`.

### Limits

- Delivery is at-least-once. A worker that dies after sending and before the
  turn is marked done repeats the reply after recovery (about 60 s later).
- A tool call is not repeated on a re-run, but only within the same turn. A
  reused lookup result can be seconds old. A call with unknown outcome is
  visible to an operator only in the logs and in `tool_executions` (rows still
  `started`).
- Rows a turn wrote before a slow call stay when the turn later fails.
- A reply row is stored after its send, so a crash between the two repeats the
  reply. A handoff the tool committed, followed by a crash before the handoff
  message is sent, leaves the customer without that message. A lock loss after
  the send still rolls back the unsupported-media and handoff reply rows.
- A retried turn's prompt contains the earlier attempt's saved tool messages.
- The API's request paths still hold a
  transaction while they fetch media.
- A send that errored but did reach the customer is retried and can repeat.
- Model errors that remain after the short retry are answered with the
  runtime's own error text, and the turn is not run again for them. Turn
  retries cover what escapes the turn: a send failure, a database
  error, a speech-to-text failure.
- The error message after the last attempt is stored first and then sent on
  a best-effort basis. On a broken channel the customer may not receive it.
- A turn cancelled after the shutdown grace period may resend bubbles it
  already sent.
- The short retry does not cover transport errors or timeouts, non-GET tools
  or sends to the channel.
- The reliability behaviour is tested with fakeredis and `respx` only, not
  against a real Redis, model or channel.
  See `docs/FOLLOW_UPS.md`.

## Known MVP limitations

- **The `/chat` widget holds the tenant API key in the browser**, so it is
  demo-only (`DEMO_MODE=true`) and not mounted by default. For real end
  customers, call the webchat channel API from your own server with the
  tenant key. An end-user-scoped token is tracked in `docs/FOLLOW_UPS.md`
  (PRD E2.6).
- **`TOOL_ALLOWED_HOSTS` in `docker-compose.yml`**: the compose file sets
  `TOOL_ALLOWED_HOSTS: mock-tools` for `api` and `worker`. The allowlist is
  authoritative, so with the default compose file every other tool endpoint
  gets 422 at create. Remove or extend the variable for real tools.
- **DNS-rebinding TOCTOU on the SSRF guard**: the allowed-IP check resolves
  the tool endpoint's hostname once before the request; `httpx` re-resolves
  it when it actually connects, so a DNS-rebinding attacker could in theory
  return a private address on that second lookup. Redirects are disabled
  (closing the much easier attack), but pinning the validated IP into the
  connection is not implemented.
- **No background memory extraction**: `remember` only fires when the model
  explicitly decides to call it mid-conversation; nothing summarizes a
  conversation into memories after the fact.
- **Long-term memory retrieval is `ILIKE`, not semantic**: `MemoryService`'s
  interface is ready for vector similarity, but today `search()` is a plain
  substring match.
- **Single retry path, no dedicated `max_iterations` status**: a capped
  agent loop is recorded as `AgentRunStatus.ERROR` with `error=
  "max_iterations"` rather than its own enum member, because the enum lives
  in a migration this scope didn't touch.
- **One agent per conversation, REST-poll only**: no multi-agent
  orchestration, no WebSocket/SSE streaming — clients poll
  `GET /conversations/{id}/messages`.

## Recommended next 3 improvements

1. **Pin the resolved IP for the SSRF-guarded request** (custom `httpx`
   transport/resolver) to close the DNS-rebinding TOCTOU gap noted above.
2. **A real `max_iterations` `AgentRunStatus` member** (needs a migration)
   instead of overloading `ERROR` with a string marker — cleaner querying
   for "did this turn actually fail" vs "did it just run out of budget".
3. **Vector-similarity long-term memory retrieval**, replacing `ILIKE`
   `search()` behind the same `MemoryService` interface, likely via the
   already-mentioned pgvector optional hook.
