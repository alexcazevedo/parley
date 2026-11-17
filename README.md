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
needs a real `LLM_API_KEY`. `tests/test_e2e_demo.py` is the automated
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
| `GET /conversations/{id}/messages` | poll for replies (`?include_tools=false` by default; `?after=<timestamp>`) |
| `POST /conversations/{id}/messages` | operator reply (typically while `HUMAN_HANDOFF`) |
| `POST /conversations/{id}/handoff` / `/resume` / `/close` | conversation state transitions |
| `GET/POST/DELETE /contacts/{id}/memories` | long-term memory CRUD |
| `GET /operator` | self-contained operator inbox page (handoff triage, reply, resume/close) |

## Evolution API (WhatsApp) setup

1. Create a channel: `POST /channels` with `type: "whatsapp"`, `config:
   {"base_url": "...", "instance": "..."}` and `secret` set to the Evolution
   instance's `apikey` (encrypted at rest, never returned by the API).
2. In Evolution, point that instance's webhook at:
   `POST https://<your-harness-host>/webhooks/whatsapp/{channel_id}`
3. Evolution must send the shared secret on every webhook call as the
   `apikey` header — the harness compares it (constant-time) against the
   channel's stored secret and returns `401` on a mismatch.
4. Outbound replies go back out through Evolution's `sendText` REST endpoint
   using that same `apikey`, `base_url` and `instance`.
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
7. Media downloaded from Evolution is read up to a cap derived from
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
