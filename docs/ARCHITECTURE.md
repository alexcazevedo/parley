# Architecture

This document is the map of the codebase for anyone extending the harness.
See `PRODUCT.md` for the product framing ("what/why") and `README.md` for
how to run it.

## Modules

```
app/
  api/            REST routers (tenants, agents, channels, tools, contacts,
                   conversations, webchat, dev, webhooks, health). Every
                   router is thin: parse/validate -> repository or service ->
                   commit -> response model. Auth deps live in
                   app/security/auth.py (X-Admin-Key, X-API-Key).
  channels/       ChannelAdapter interface + two implementations: webchat
                   (REST only) and evolution (WhatsApp). parse_incoming/
                   send_message is the whole seam.
  messages/       ingest() — the one place inbound messages are deduplicated
                   and turned into a queue Job. Every entry point (webchat,
                   dev, whatsapp webhook) calls this, never bypasses it.
  conversations/  resolver (contact+conversation lookup/creation), debounce
                   (Redis pending list + due ZSET), lock (per-conversation
                   advisory lock), turn (process_turn: one debounced batch ->
                   one reply), summary (rolling conversation summary).
  agents/         context.py (ContextBuilder: system prompt + history ->
                   LLMMessage[]), runtime.py (AgentRuntime: the tool-call
                   loop, one AgentRun per turn).
  llm/            base.py (provider-agnostic dataclasses/Protocol),
                   factory.py (Settings -> provider), providers/
                   (openai_compatible.py — the only wire format; fake.py —
                   scripted provider for tests/offline demos).
  tools/          base.py (ToolExecutor Protocol, ToolResult, ToolContext),
                   registry.py (merges executors, first-match wins),
                   builtin.py (remember, handoff_to_human — harness-provided),
                   http.py (the tenant's own HTTP tools), ssrf.py (outbound
                   URL guard), validation.py (JSON Schema arg validation).
  handoff/        HandoffService — the only place conversation state moves
                   in/out of HUMAN_HANDOFF.
  memory/         MemoryService — long-term facts about a contact.
  security/       auth (API key hashing/lookup), secrets (Fernet encryption
                   for tool auth_config / channel secrets), ratelimit,
                   limits (message size).
  queue/          Queue Protocol + RedisQueue (BRPOPLPUSH-alike job list +
                   a dead-letter list).
  workers/        main.py (job loop: dequeue -> push onto the conversation's
                   debounce batch -> ack), scheduler.py (tick: drains due
                   conversations into turns; run_scheduler runs it forever).
  models/         SQLAlchemy 2 declarative models, one file per table.
  repositories/   BaseRepository (get/list/create/update/delete, tenant_id
                   first) + per-model subclasses.
  observability/  structlog config + request-id middleware.
```

## Data model

All tenant-owned tables inherit `TenantScoped` (`tenant_id` first column,
indexed) — the shape PostgreSQL RLS would key off later; today isolation is
enforced by every repository/query filtering on `tenant_id` explicitly.

```
tenants (admin-created; api_key_hash)
  └─ agents (instructions, model, provider, tool_names[], memory_enabled,
             handoff_enabled, max_tool_iterations, context_message_limit,
             summary_threshold, ...)
  └─ channels (type: webchat|whatsapp, agent_id, config, secret_encrypted)
       └─ contacts (external_user_id, per channel)
            └─ conversations (agent_id, channel_id, contact_id, state,
                               summary, summary_cursor, last_message_at)
                 ├─ messages (direction, role, type, content, raw_payload,
                 │            agent_run_id?, tool_call_id?, external_message_id)
                 ├─ agent_runs (one per turn: status, llm_calls, tool_calls,
                 │              prompt/completion_tokens, latency_ms, error)
                 │    └─ tool_executions (tool_id, arguments, result, status,
                 │                        latency_ms, error)
                 └─ handoffs (reason, requested_by, resolved_at)
  └─ tools (name, input_schema, endpoint, method, auth_config_encrypted,
            timeout_seconds)
  └─ memories (contact_id, type, content, importance, last_accessed_at,
               expires_at) — long-term facts, independent of any one
               conversation
```

`ConversationState`: `ACTIVE_AI -> HUMAN_HANDOFF -> ACTIVE_AI` (via
`handoff_to_human`/`/handoff` and `/resume`), or `-> CLOSED` (`/close`,
terminal). `process_turn` only runs the agent when `state == ACTIVE_AI`; any
other state means "a human is driving it, stay quiet" — new messages still
persist, they just don't trigger a turn.

## Queue / debounce algorithm

Goal: a burst of messages on one conversation becomes exactly one agent turn,
without losing any message and without two workers double-processing the
same conversation.

1. `ingest()` persists the message (idempotent on
   `(tenant_id, channel_id, external_message_id)`) and pushes a `Job` onto
   `RedisQueue` (`harness:jobs`, with `harness:dlq` for exhausted retries).
2. The worker's job loop (`app/workers/main.py::run_once`) dequeues one `Job`
   at a time, appends its `message_id` to
   `harness:pending:{conversation_id}` (a Redis LIST), and (re)writes
   `harness:due` (a ZSET) with `conversation_id -> now + DEBOUNCE_SECONDS`.
   Every new message in the burst pushes the due score further out, so the
   window keeps sliding until messages stop arriving.
3. The scheduler task (`app.workers.scheduler.run_scheduler`, a 250ms loop
   calling `tick`) pops every conversation whose due score has elapsed
   (`ZRANGEBYSCORE ... -inf now`), and for each one:
   - `ZREM`s it from `harness:due` (claim — only one worker wins the race);
   - acquires `harness:lock:{conversation_id}` (`SET NX PX`, compare-and-
     delete release via WATCH/MULTI); if another worker already holds it,
     the conversation is re-armed a second out instead of dropped;
   - atomically drains `harness:pending:{conversation_id}` (`LRANGE` +
     `DEL` in one `MULTI/EXEC`, so nothing arriving mid-drain is lost);
   - loads those messages, joins their content, and calls
     `process_turn(session, conversation_id, messages, runtime)`;
   - releases the lock; if more messages arrived on the pending list while
     the lock was held, re-schedules the conversation immediately for
     another turn.
4. `process_turn` persists the assistant's reply in the same transaction the
   runtime used, sends it through the `ChannelAdapter`, updates
   `last_message_at`, and best-effort-advances the rolling summary.

## Agent loop

`AgentRuntime.run_turn` (a `SessionAwareTurnRuntime`, injected into
`process_turn` by the worker) is one `AgentRun` row per turn:

1. Build the prompt: `ContextBuilder` = system prompt (`agent.instructions` +
   long-term memory facts + the rolling summary) + the last
   `agent.context_message_limit` messages, replayed with tool-call/result
   pairs reconstructed from `Message.raw_payload`.
2. Loop up to `agent.max_tool_iterations` times:
   - call the `LLMProvider` with the transcript + advertised `ToolSpec`s;
   - no tool calls -> that's the final answer, `AgentRunStatus.SUCCESS`;
   - tool calls -> persist a `tool_call` message, execute each via
     `ToolRegistry.execute` (first executor whose `can_handle` matches;
     builtins before the tenant's HTTP tools), persist a `tool_result`
     message per call, and feed the results back to the model — unless a
     result carries `ToolResult.meta["terminal"]` (currently only
     `handoff_to_human`), which ends the loop immediately with
     `meta["final_text"]` as the answer, skipping the extra model round trip.
3. Exhausting the iteration cap without a final answer is recorded as
   `AgentRunStatus.ERROR` / `error="max_iterations"` (no dedicated enum
   member — see the note in `app/agents/runtime.py`) and answers with a
   fixed "couldn't finish" message. A provider exception is caught the same
   way, with a fixed "technical problem" message — a broken turn must not
   cost the user a reply.

## Memory

Two kinds, deliberately different:

- **Short-term**: the tail of the persisted `messages` table
  (`context_message_limit`), plus a rolling `conversation.summary` that
  `app/conversations/summary.py` advances once
  `messages_since_last_summary >= agent.summary_threshold` — an LLM call that
  folds the new messages into the prior summary and never runs synchronously
  in the reply path (it happens after the reply is already persisted, and
  never raises out of `process_turn`).
- **Long-term**: `Memory` rows per contact (`type`, `content`, `importance`),
  written by the agent through the `remember` built-in tool or by a customer
  via `POST /contacts/{id}/memories`. Retrieval
  (`MemoryService.get_contact_memories`) is folded into the system prompt on
  every turn (gated by `agent.memory_enabled`) and is plain structured SQL
  today — `search()` is an `ILIKE`; the interface is the seam a
  vector-similarity implementation would slot into without touching callers.

No background extraction job: memory is only ever written when the model
explicitly decides to call `remember`.

## Tool model

Two sources merged by `ToolRegistry`, in priority order:

1. **Built-in** (`app/tools/builtin.py`): `remember` and `handoff_to_human`,
   table-driven (`(ToolSpec, agent-flag)` pairs), individually gated by an
   `Agent` boolean column so a tenant can turn either off per agent.
2. **HTTP** (`app/tools/http.py`): the tenant's own `Tool` rows. `can_handle`
   claims every name (it needs a `ToolContext`/session to know what actually
   exists), so it is always registered last. Schema-validates arguments
   (`app/tools/validation.py`) before ever making a request, guards the
   endpoint against SSRF (`app/tools/ssrf.py` — blocks private/loopback/
   link-local/reserved addresses and disallows redirects; a
   `TOOL_ALLOWED_HOSTS` allowlist bypasses the IP check for known-safe
   internal hosts, e.g. the demo's `mock-tools`), and records a
   `ToolExecution` row (arguments, result, status, latency, error) whether it
   succeeds or fails. Every failure mode — invalid arguments, SSRF block,
   timeout, HTTP error, non-2xx response — comes back as
   `ToolResult(ok=False, error=...)`, never an exception the runtime has to
   catch: the model reads the error and can apologize, retry a different
   way, or hand off.

`ToolResult.meta` is also the terminal-tool mechanism: any executor can set
`meta={"terminal": True, "final_text": "..."}` to end the loop immediately
instead of returning control to the model — `handoff_to_human` is the only
current user.

## Extension points

- **New channel**: implement `ChannelAdapter` (`app/channels/base.py`),
  register it in `app/channels/registry.py`, add a webhook route if it's
  push-based. `Telegram`/`Slack`/etc are explicitly out of scope for now but
  this is the seam (see PRODUCT.md).
- **New LLM provider**: today only `OpenAICompatibleProvider` exists;
  `app/llm/factory.py::get_provider` is the one dispatch point — a new
  branch there plus a class implementing `LLMProvider.complete` is the whole
  change.
- **New built-in tool**: add a `(ToolSpec, agent-flag)` pair to
  `BUILTIN_TOOLS` and a handler to `_HANDLERS` in `app/tools/builtin.py`; no
  registry/runtime change needed.
- **Semantic memory**: `MemoryService.search` is the seam — swap the `ILIKE`
  for a vector-similarity query without touching `app/tools/builtin.py` or
  `app/agents/runtime.py`.
- **Operator dashboard**: the REST surface
  (`GET/POST /conversations/{id}/messages`, `/handoff`, `/resume`, `/close`)
  is already dashboard-ready; only a UI is missing (out of scope per
  PRODUCT.md).
