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
# then edit .env: set a real LLM_API_KEY (OpenAI or any OpenAI-compatible
# endpoint — OpenRouter, Ollama, vLLM, ...) for the live demo. Everything
# else in .env.example has a working default for local Docker Compose.

docker compose up -d --build
# starts postgres, redis, api (:8001 on the host, runs migrations on boot),
# worker, and mock-tools (:9000 inside the compose network — the demo's
# fake business endpoint).

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
| `POST /agents`, `/channels`, `/tools` | configure an agent, a channel, a business tool |
| `POST /channels/webchat/messages` | Web Chat ingestion (the only channel a customer's own widget talks to) |
| `POST /dev/messages` | dev simulator — same pipeline as Web Chat, for scripting demos |
| `POST /webhooks/whatsapp/{channel_id}` | Evolution API inbound webhook |
| `GET /conversations/{id}/messages` | poll for replies (`?include_tools=false` by default; `?after=<timestamp>`) |
| `POST /conversations/{id}/messages` | operator reply (typically while `HUMAN_HANDOFF`) |
| `POST /conversations/{id}/handoff` / `/resume` / `/close` | conversation state transitions |
| `GET/POST/DELETE /contacts/{id}/memories` | long-term memory CRUD |

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

## Known MVP limitations

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
