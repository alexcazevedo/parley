# Architecture

This document is the map of the codebase for anyone extending parley.
See `PRODUCT.md` for the product framing ("what/why") and `README.md` for
how to run it.

## Modules

```
app/
  api/            REST routers (tenants, agents, channels, tools, contacts,
                   conversations, webchat, dev, webhooks, health, chat,
                   operator). `chat` and `dev` are mounted only when
                   `DEMO_MODE=true` (`create_app`); otherwise they 404. Every router is thin: parse/validate ->
                   repository or service -> commit -> response model. Auth
                   deps live in app/security/auth.py (X-Admin-Key, X-API-Key).
                   operator.py serves the self-contained operator inbox page
                   (app/static/operator.html) at GET /operator — no API auth
                   on the route itself, since the page gates on X-API-Key in
                   JS against the endpoints below. media.py serves
                   GET /media/{message_id} (tenant-scoped file download for
                   image/audio messages).
  channels/       ChannelAdapter interface + two implementations: webchat
                   (REST only) and evolution (WhatsApp). parse_incoming/
                   send_message is the whole seam.
  messages/       ingest() — the one place inbound messages are deduplicated
                   and turned into a queue Job. Every entry point (webchat,
                   dev, whatsapp webhook) calls this, never bypasses it.
  conversations/  resolver (contact+conversation lookup/creation), debounce
                   (Redis pending list + due ZSET + claim + in-flight list),
                   lock (per-conversation lock, renewed while a turn runs),
                   retry (failed-turn counter, turn dead-letter list),
                   turn (process_turn: one debounced batch -> one reply),
                   summary (rolling conversation summary).
  agents/         context.py (ContextBuilder: system prompt + history ->
                   LLMMessage[]), runtime.py (AgentRuntime: the tool-call
                   loop, one AgentRun per turn).
  llm/            base.py (provider-agnostic dataclasses/Protocol),
                   factory.py (Settings -> provider), providers/
                   (openai_compatible.py — the only wire format; fake.py —
                   scripted provider for tests/offline demos).
  tools/          base.py (ToolExecutor Protocol, ToolResult, ToolContext),
                   registry.py (merges executors, first-match wins),
                   builtin.py (remember, handoff_to_human — parley-provided),
                   http.py (the tenant's own HTTP tools), ssrf.py (outbound
                   URL guard), validation.py (JSON Schema arg validation).
  media/          Channel-independent media pipeline: storage.py (local
                   filesystem, tenant-scoped), stt.py (Whisper-compatible
                   transcription), prepare.py (prepare_media_messages — runs
                   STT on freshly-ingested audio before the turn joins the
                   batch into text).
  handoff/        HandoffService — the only place conversation state moves
                   in/out of HUMAN_HANDOFF.
  memory/         MemoryService — long-term facts about a contact.
  security/       auth (API key hashing/lookup), secrets (Fernet encryption
                   for tool auth_config / channel secrets), ratelimit
                   (Redis counters, atomic; per-user then tenant-wide limits
                   via enforce_inbound_limits — webchat answers 429, the
                   WhatsApp webhook answers 200 `rate_limited` and stores
                   the message without enqueuing a turn),
                   limits (message size).
  queue/          Queue Protocol + RedisQueue (job list; a dequeued job moves
                   with BLMOVE into the worker's own processing list and
                   stays there until acked; dead workers' lists are put
                   back; plus a dead-letter list).
  workers/        main.py (job loop: dequeue -> push onto the conversation's
                   debounce batch -> ack; housekeeping loop; shutdown),
                   scheduler.py (run_scheduler: keeps a pool of turn tasks, up to
                   WORKER_CONCURRENCY at once, and fills free slots every
                   250 ms; tick is the run-to-completion variant tests use).
  models/         SQLAlchemy 2 declarative models, one file per table.
  repositories/   BaseRepository (get/list/create/update/delete, tenant_id
                   first) + per-model subclasses.
  observability/  structlog config + request-id middleware (see "Log
                   context").
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
                               summary, summary_last_seq, last_message_at,
                               category_id?, category_locked, waiting_since?,
                               claimed_by?, claimed_at?)
                 ├─ messages (seq, direction, role, type, content, raw_payload,
                 │            agent_run_id?, tool_call_id?, external_message_id)
                 ├─ agent_runs (one per turn: status, llm_calls, tool_calls,
                 │              prompt/completion_tokens, latency_ms, error)
                 │    └─ tool_executions (tool_id, arguments, result, status,
                 │                        latency_ms, error)
                 └─ handoffs (reason, requested_by, summary, resolved_at)
  └─ categories (name, description; unique per tenant)
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

**Message order.** A conversation is read in `Message.seq` order: a value from
the database sequence `messages_seq`, taken at insert (migration `0013`).
History, the rolling summary, the sentiment check, the handoff brief, the
scheduler's batch and `GET /conversations/{id}/messages` all order by it.
`created_at` is for display only: inbound rows get it from the database
(`now()`), assistant, tool and operator rows from the app (`datetime.now(UTC)`),
so two machines whose clocks differ stamp a reply earlier or later than the
customer message it answers, while `seq` stays in insertion order. The one place
that still compares `created_at` is the `after=` filter of the messages
endpoint.

**Summary cursor and the row lock.** `conversation.summary_last_seq` is the
`seq` of the last message the rolling summary covered; the next summary takes
the messages with a higher `seq`. A sequence value is taken before its row
becomes visible, so a transaction could commit a lower `seq` after the summary
had moved past it and the message would be skipped. To prevent that, the
inserts of a customer message (`ingest`) and of an operator message
(`post_operator_message`) lock the conversation row (`SELECT ... FOR UPDATE`)
before inserting and hold it until they commit, and the summarizer takes the same
lock before reading the cursor and the pending rows. Replies of a turn are
written inside the turn, which does not take this lock itself.
It releases it (`end_transaction`) before the model call and writes the new
cursor afterwards. The operator message is sent first and the lock taken only to
store it, so the lock is never held across a send, and a failed send stores
nothing.

**Warm-handoff brief**: the `handoff_to_human` tool goes through
`app.handoff.summarizer.handoff_with_summary`; the operator-initiated
`POST /conversations/{id}/handoff` calls `HandoffService.handoff`, commits,
then `generate_handoff_summary`. `handoff_with_summary` performs the state transition, then
makes one best-effort LLM call (`generate_handoff_summary`, timeout-bounded
by `SUMMARY_TIMEOUT_SECONDS`) that reads recent messages, contact memories,
the rolling conversation summary and the handoff reason to write a compact
operator brief (who/what/why/tone, in the conversation's language), and
stores it on `handoffs.summary`. Any LLM failure or timeout is logged and
swallowed — the escalation itself is never blocked, and `summary` is simply
left `null`. `GET /conversations/{id}` (single-GET only, not the list
endpoint) surfaces the newest handoff row for the conversation as
`latest_handoff: {reason, summary, requested_by, created_at} | null`, so an
operator opening a handed-off conversation reads the brief before sending
their first message. The summary never enters the message transcript, so the
end-user-facing channel/webchat poll never sees it. See
`app/handoff/summarizer.py`.

## Queue / debounce algorithm

Goal: a burst of messages on one conversation becomes exactly one agent turn,
without losing any message and without two workers double-processing the
same conversation.

1. `ingest()` persists the message (idempotent on
   `(tenant_id, channel_id, external_message_id)`) and pushes a `Job` onto
   `RedisQueue` (`harness:jobs`, with `harness:dlq` for exhausted retries).
2. The worker's job loop (`app/workers/main.py::run_once`) takes one `Job` at
   a time. `dequeue` uses `BLMOVE` to move it from `harness:jobs` into the
   worker's own list `harness:processing:{worker_id}` in one step, so a job
   is always in exactly one list. The loop appends the `message_id` to
   `harness:pending:{conversation_id}` (a Redis LIST), (re)writes
   `harness:due` (a ZSET) with `conversation_id -> now + DEBOUNCE_SECONDS`,
   and only then `ack`s the job (removes it from the processing list).
   Every new message in the burst pushes the due score further out, so the
   window keeps sliding until messages stop arriving.
   Each worker also refreshes `harness:worker:{id}` (TTL 30 s) and is listed
   in `harness:workers`. `housekeeping_loop` does this every 10 s and calls
   `reap_dead_workers`, which moves every job in the processing list of a
   worker whose alive key has expired back onto `harness:jobs` (oldest
   first). A worker that was only slow may be reaped and its job handled
   twice; `drain_pending` returns each message id once, so that is harmless.
3. The scheduler task (`app.workers.scheduler.run_scheduler`) keeps a pool
   of turn tasks. Every 250 ms it fills the free slots: it looks at every
   conversation whose due score has elapsed (`ZRANGEBYSCORE ... -inf now`)
   and starts a turn for each it claims, without waiting for the turns
   already running. (`tick` is the run-to-completion variant, used by most
   tests; the worker does not call it.) Up to `WORKER_CONCURRENCY` conversations
   (default 4) run at once, each as its own task with its own database
   session; a conversation is claimed only when a slot is free. For each:
   - **claim**: `debounce.claim` takes the key `harness:claim:{id}` with
     `SET NX PX` (60 s, `CLAIM_TIMEOUT_SECONDS`) and moves the due score to
     `now + 60 s`; only the worker that gets the key wins. The
     entry is not removed, so a worker that dies from here on leaves the
     conversation due again after the timeout;
   - acquires `harness:lock:{conversation_id}` (`SET NX PX`, 60 s TTL,
     compare-and-delete release via WATCH/MULTI); if another worker already
     holds it, the conversation is re-armed a second out instead of dropped.
     The same conversation therefore never runs twice at once;
   - **in-flight list**: `debounce.drain_pending` moves
     `harness:pending:{conversation_id}` onto `harness:inflight:{conversation_id}`
     in one `MULTI/EXEC` (nothing arriving mid-drain is lost) and returns the
     whole in-flight list, so a batch a dead worker left behind comes back
     ahead of newer messages;
   - **lock heartbeat**: while the turn runs, a task renews the lock and
     pushes the claim out every 20 s (`LOCK_RENEW_INTERVAL_SECONDS`). Before
     every send and before its commit the turn asks whether the lock is still
     its own. If not, it rolls back, sends nothing more and returns
     `LockLost`; the scheduler then writes no debounce state at all, because
     whoever holds the lock now owns the batch (and if nobody does, the claim
     timeout brings it back);
   - loads those messages, joins their content, and calls
     `process_turn(session, conversation_id, messages, runtime,
     supersede_check=check, ownership_check=...)`, where `check` probes
     `debounce.has_pending(redis, conversation_id)` — has a newer message
     landed on the pending list since the drain? If the turn comes back
     `Superseded`, the in-flight batch is put back at the head of the pending
     list via `debounce.return_inflight` (ahead of whatever interrupted it)
     instead of being counted as processed — see "Barge-in" below;
   - **retry**: if the turn fails (`TurnFailed`, or an error from the session
     or the batch load), `_settle_failure` counts it in
     `harness:turn_attempts:{conversation_id}`. Below `MAX_TURN_ATTEMPTS` (3)
     it calls `return_inflight` and schedules the conversation after the
     backoff (`RETRY_BACKOFF_SECONDS`: 2 s, then 8 s). At the limit it appends
     an entry to `harness:dlq:turns`, calls `finish`, and then stores and
     sends the agent's `error_message` (else `TURN_FAILED_MESSAGE`) once. If part of the reply was already
     delivered it dead-letters the batch at once (`partly_delivered` true),
     with no retry and no error message;
   - **finish**: `debounce.finish` drops the in-flight list and removes the
     due entry, or makes the conversation due now if more messages arrived on
     the pending list meanwhile (including a returned superseded batch). It
     runs under the lock; then the lock is released. An error from the Redis
     bookkeeping itself skips `finish`: the batch stays in flight and comes
     back after the claim timeout.

   `tick` runs the conversations side by side and waits for all of them;
   `run_scheduler` keeps its pool across intervals. When its `stop` event is set
   (on `SIGTERM`/`SIGINT`) it claims nothing new and waits for the running
   turns; `main` gives them `SHUTDOWN_GRACE_SECONDS` (90) and then cancels
   what is left.
4. `process_turn` persists the assistant's reply in the same transaction the
   runtime used, sends it through the `ChannelAdapter`, updates
   `last_message_at`, and best-effort-advances the rolling summary.

## Agent loop

`AgentRuntime.run_turn` (a `SessionAwareTurnRuntime`, injected into
`process_turn` by the worker) is one `AgentRun` row per turn:

1. Build the prompt: `ContextBuilder` = system prompt (`agent.instructions` +
   long-term memory facts + the rolling summary) + the history, replayed with
   tool-call/result pairs reconstructed from `Message.raw_payload`. The history
   is chosen by `AgentRuntime._recent_messages`: `agent.context_message_limit`
   (20 when unset) counts `USER`, `HUMAN_OPERATOR` and `ASSISTANT` rows that are
   not `tool_call`, and the history is every row from the limit-th most recent
   counted one on, tool rows included. `ContextBuilder.build` does not cut again.
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
   way; the reply is the agent's `error_message`, or the default "technical
   problem" text when it is null — a broken turn must not cost the user a
   reply.

### A turn is a series of short transactions

A turn does not hold a database transaction across slow work. Before every
model call (the reply check included), tool call, speech-to-text call, send to
the channel and paced wait, the code calls `app.db.end_transaction(session)`,
which commits the open transaction if there is one. The connection goes back
to the pool while the call runs. Sessions use `expire_on_commit=False`, so
loaded rows stay usable. The cost: what was written before the call is
committed and stays if the turn fails later. The time limit
(`asyncio.wait_for`) wraps only the model call, never a database operation or
a tool, so a timeout cannot leave the session mid-statement. A model step's
tool messages are added to the session only after every call of the step
returned. Intermediate commits are not preceded by a lock check; the lock is
checked before every send and before the commits that store a turn's outcome. The API's request paths still hold a
transaction while they fetch media.

### The tool ledger

`app/tools/ledger.py` puts every tool call on record before it runs.
`begin` inserts a `ToolExecution` with status `STARTED` and commits it;
`finish` fills in the result (`model_content` holds the exact string the model
received) and commits again, together with anything the tool wrote on the
session (a built-in's `Memory` or `Handoff`). A row left `STARTED` means the
outcome is unknown. `HttpToolExecutor` also leaves it `STARTED`, through
`ledger.mark_unknown` (fills in `error` and `latency_ms`), when the call failed
after its request may have reached the endpoint: an `httpx.TimeoutException`
other than `ConnectTimeout`, an `httpx.HTTPError` other than `ConnectError`,
or an unexpected exception. A blocked endpoint (`SSRFError`), `ConnectError`,
`ConnectTimeout`, invalid arguments and any HTTP response are finished as
today. In every case the current attempt's model gets the same failure text.
`finish` stores a terminal result's `final_text` (`handoff_to_human`) in
`terminal_text`, and `replay` restores `terminal=True` and `final_text`, so a
reused handoff still ends the loop. `agent_runs.conversation_id` and
`tool_executions.agent_run_id`, which `find_prior` filters and joins on, are
indexed.

`ToolRegistry.execute` asks `ledger.find_prior` first. "Same turn" is decided
by the batch: `process_turn` passes the ids of the messages it answers to
`run_turn`, which stores them in `AgentRun.input_message_ids`. An earlier run
counts when it is in the same tenant and conversation and its ids are a
non-empty subset of the current run's ids. A retry has the same ids, a turn
merged with newer messages has a superset, a later turn has none of them.
The match is on tool name and arguments; a finished row (the newest) wins over
a `STARTED` one.

- finished (`OK` or `ERROR`): `ledger.replay` returns the saved result, the
  tool does not run, no new row is written (`tools.result_reused`);
- `STARTED`: the call runs again only if the owning executor's
  `repeat_safe(name, ctx)` is true (HTTP tool with GET; built-ins, whose effect
  and result are saved together). Otherwise the model gets
  `OUTCOME_UNKNOWN_ERROR` and the log gets `tools.outcome_unknown`. An
  executor without `repeat_safe` counts as not safe.

### The shared HTTP client

`app/http_client.py` holds one `httpx.AsyncClient` per process, created on
first use (`shared_client()`). The model provider, speech-to-text, the
Evolution adapter and the HTTP tools all use it, each passing its own timeout
per request; redirects stay off. The API's lifespan (`app/main.py`) and the
worker's `_close_connections` (`app/workers/main.py`) close it through
`close_shared_client()` on shutdown; the provider's own `aclose` does not.
`app/http_retry.py` adds the short retry on 429/5xx for model calls,
speech-to-text and GET tools (`send_with_retry`); a GET tool repeats its SSRF
check before every try.

### Tenant-scoped agent loading

Every place that needs the agent of a conversation (`process_turn`,
`AgentRuntime.run_turn`, the rolling summary in `app/conversations/turn.py`
and `generate_handoff_summary` in `app/handoff/summarizer.py`) goes through
`load_conversation_agent` (`app/agents/loading.py`). It returns the agent only
when it belongs to the conversation's tenant. Otherwise it logs
`agent.tenant_mismatch` (conversation, agent and both tenant ids) and returns
`None`. Then `process_turn` logs `turn.refused_agent_unavailable` and returns
`None`: nothing is sent (not even a read receipt), no `AgentRun` is written
and nothing is committed. `run_turn` raises `RuntimeError`, and the rolling
summary and the handoff brief are skipped without a model call. The API
refuses to create such links; they can only exist in rows written by an older
version. `python -m app.audit` lists them (see README, "After upgrading").

### Boundaries in a turn

Boundaries (see README "Agent boundaries") sit at two points of a turn:

1. **Prompt**: `effective_boundaries` (`app/agents/boundaries.py`) merges the
   operator floor with the agent's own boundaries, and `render_boundaries`
   writes them into the system prompt as a `## Boundaries` block.
2. **Check before delivery**: when the loop reaches a model-written final
   answer, `AgentRuntime._enforce_boundaries` (`app/agents/runtime.py`) runs
   `check_reply`, one extra LLM call. It is skipped when there are no
   boundaries or the check is off. A `BLOCK` (or an error, timeout or
   unparseable answer) swaps the reply for the refusal message; the original
   text goes to `agent_runs.blocked_text`. Only the checked text, or the
   refusal, reaches the send path, so the humanizer and barge-in never see
   the blocked reply.

## Memory

Two kinds, deliberately different:

- **Short-term**: the tail of the persisted `messages` table, counted in
  customer, operator and assistant messages (`context_message_limit`; the tool
  rows of those turns come along), plus a rolling `conversation.summary` that
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
  Reading writes nothing. The order is `importance DESC, created_at DESC, id`,
  and the agent gets the first `MEMORY_CONTEXT_LIMIT` rows (default 20).
  `MemoryService.store` is the one write path (the tool and the API both use
  it): it trims the content, collapses whitespace and lower-cases it, and if a
  non-expired memory of the same contact has the same text it returns that row
  and raises its importance to the higher value, instead of inserting.
  `last_accessed_at` is still a column and in `MemoryOut`, but nothing writes
  it.

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
   endpoint against SSRF (`app/tools/ssrf.py` — only globally routable
   addresses are allowed and redirects are disallowed; the endpoint is also
   validated, without DNS, when a tool is created or updated — that create-time
   check rejects `localhost`/`*.localhost`/`*.local`/`*.internal` names and
   non-global IP literals in any IPv4 notation; other hostnames are checked
   only at call time, when every resolved address must be global. Requests
   send `Accept-Encoding: identity` and a response with another
   `Content-Encoding` fails as `unsupported response encoding`. Settings come
   from `get_settings()`: when `TOOL_ALLOWED_HOSTS` is set it is
   authoritative — listed hosts skip the IP check, e.g. the demo's
   `mock-tools`, and all other hosts are refused), and records a
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

## Humanizer

Makes replies feel human, not bot-instant. Enabled per channel via
`config: {"humanize": true}`, the humanizer is a send-path wrapper in
`app/conversations/humanize.py` that:

1. **Read receipt & presence** (WhatsApp/Evolution only): marks the inbound
   message read and sends "composing" presence, via optional `mark_read()` and
   `send_typing()` hooks on `ChannelAdapter` (no-op by default; Evolution
   implements both via its REST API).
2. **Split replies**: breaks long replies into 2–4 short bubbles on blank
   lines (agent instruction tells the model to write chat-style messages);
   falls back to sentence-split if blank-line parsing fails. Never splits
   code blocks or mid-word. Zero extra LLM calls — the split is mechanical.
3. **Paced delivery**: delays between bubbles proportional to their length,
   using configurable typing speed (chars/sec) and min/max per-bubble delay
   (global `Settings`). Web Chat persists staggered parts (one per DB commit)
   so polling renders them over seconds; Evolution sends each part immediately
   via `sendText`, each preceded by a `sendPresence` composing update.

When `humanize: false` (or absent), the legacy single-message path runs
byte-for-byte as before. Delays happen within the conversation turn (lock
already held) — they only slow that one conversation, never the worker's
other jobs. Failures in read/typing calls are best-effort: never cost the user
their reply.

Hook points: `ChannelAdapter.mark_read()`, `ChannelAdapter.send_typing()` in
`app/channels/base.py`; integration hooks in `app/channels/evolution.py`.

## Barge-in

A message arriving mid-turn stops the stale reply instead of queuing a second
answer behind it. `app/conversations/turn.py::process_turn` checks a
`supersede_check` probe at exactly two safe checkpoints — once after the
runtime returns and before any part is sent, and again between humanizer
bubbles — never mid-LLM or mid-tool-call, so nothing is aborted while it has
side effects in flight.

- **Checkpoints only**: no cancellation during the LLM call or tool
  execution; a hit is only acted on at the two points above.
- **Sent bubbles stand**: a checkpoint 2 hit drops the unsent parts, but
  whatever already went out stays out — same as a human getting cut off
  mid-thought.
- **Observable, not an error**: the turn's `AgentRun` (if the runtime wrote
  one) is flipped to `AgentRunStatus.SUPERSEDED` (an `ERROR` run keeps its
  error status), and `process_turn` returns `Superseded(delivered_parts)`
  instead of a `Message`.
- **Requeue -> one merged reply**: `app/workers/scheduler.py::tick` passes a
  probe backed by `debounce.has_pending(redis, conversation_id)` (the
  existing pending list — no new transport) and, on `Superseded`, calls
  `debounce.return_inflight` to put the in-flight batch back at the head of the
  pending list, ahead of whatever interrupted it. The scheduler's normal
  re-arm (see "Queue / debounce algorithm" step 3) fires the very next tick,
  which drains old + new together and answers them as one reply.
- **Degrade to today on probe failure**: `_is_superseded` never lets a
  probe exception escape — a broken signal is treated as "not superseded",
  so the reply is delivered in full and the interrupting message is simply
  answered by the next turn, exactly like before this feature existed.
- **Escalation path exempt**: `_maybe_escalate_on_sentiment`'s handoff
  message is never a stale AI answer, so it is delivered whole with no
  supersede checkpoints — a conversation already moving to `HUMAN_HANDOFF`
  must not have its handoff message shredded mid-flight.

### State check before each send

An operator can hand off, resume or close a conversation while a turn runs.
`HandoffService` decides on the row read `FOR UPDATE`, and the operator
handoff endpoint commits the state change before it generates the brief, so
the operator never waits for the turn. Right before every send (the single
message, and each humanizer bubble) `app/conversations/turn.py::_state_change`
reads the conversation's state column from the database. If it is no longer
AI-driven, the turn rolls back, logs `turn.stopped_state_changed`
(`conversation_id`, `state`, `delivered_parts`), and returns the last
delivered reply or `None`. Bubbles already sent stay; the rest is dropped.
Differences: the lock check asks whether this worker still owns the
conversation; the supersede check asks whether a newer customer message
arrived (and requeues the batch); the state check asks whether someone
changed the conversation's state. A tool call in flight is not interrupted.

A handoff made by the turn's own `handoff_to_human` tool is delivered whole
with no supersede checkpoint, and a superseded turn marks only the
`AgentRun` it created.

### One open conversation per contact and channel

`uq_conversations_open_contact_channel` is a partial unique index on
`(tenant_id, contact_id, channel_id)` where `state != 'CLOSED'`
(migration `0012`). `app/conversations/resolver.py` creates contacts and
conversations with select, `INSERT ... ON CONFLICT DO NOTHING`, select again
(up to 3 tries), so two simultaneous first messages share one contact and one
conversation. Migration `0012` first closes existing duplicates, keeping the
one a human handles, else the most recently active.

## Sentiment escalation

Proactive handoff when a user gets angry, without them having to ask for a
person. Enabled per agent via `agents.sentiment_escalation_enabled` (default
off; the `/chat` demo agent on).

1. **Classifier** (`app/agents/sentiment.py`): one cheap LLM call scoring the
   debounced inbound batch plus the last 10 messages for user frustration,
   0.0–1.0. Runs in `process_turn` after the `ACTIVE_AI` state check and
   *before* the runtime, so a frustrated batch never gets an AI answer.
2. **Threshold**: global `Settings.sentiment_threshold` (0.7) and
   `sentiment_timeout_seconds` (5.0) — no per-tenant/per-channel overrides.
3. **Trip** (`app/conversations/turn.py::_maybe_escalate_on_sentiment`): the
   turn short-circuits — the agent runtime and the rolling summary are both
   skipped; the conversation goes to `HUMAN_HANDOFF` through the same warm
   handoff as everything else (`handoff_with_summary`, so the operator brief
   is generated) with `requested_by=system` and a reason citing the score;
   the agent's `handoff_message` is delivered as the reply through the normal
   send path, so humanizer pacing still applies.
4. **Best-effort**: the classifier never blocks a turn. Any error, timeout or
   unparseable answer scores `None`, which is treated as "no signal" and the
   normal agent turn proceeds. Only `ACTIVE_AI` conversations are scored, so
   an already handed-off conversation can't double-escalate.

## Categories and waiting lines

Parley stores the facts: a conversation's category, since when it waits
and who claimed it. Operators, line visibility and priorities belong to the
app that uses it.

**Columns.** Migration `0014`: table `categories` (unique `(tenant_id, name)`),
`conversations.category_id` (nullable, foreign key `ON DELETE SET NULL`, indexed)
and `conversations.category_locked` (default false). Migration `0015`:
`waiting_since`, `claimed_by` (text), `claimed_at`, and the partial index
`ix_conversations_waiting` on `(tenant_id, waiting_since)` where
`state = 'HUMAN_HANDOFF'`.

**The classifier in the turn** (`app/agents/categorize.py`). One model call per
batch, modelled on the sentiment classifier. In `_run_turn` it runs right after
the batch text is built and before the sentiment check, so an escalated batch is
already filed when it is handed off. It also runs in the branch that skips a
turn because the conversation is not `ACTIVE_AI`, only for `HUMAN_HANDOFF`, so
the category follows the subject while a human has it; `WAITING` and `CLOSED`
are not categorized. The model gets the categories as a numbered list (by name)
and answers a number, `0` for none. Only a valid new number is written. It is
best-effort and never raises: no model call is made for a locked conversation or
a tenant with no categories; the reads run in a savepoint (a missing table does
not fail the turn); no transaction is open during the call; a timeout, provider
error, bad answer or a category deleted meanwhile leaves the row unchanged. The
write is one `UPDATE ... WHERE category_locked IS FALSE`, so a human's choice
made during the call is never overwritten. In that case nothing is written and
nothing is logged.

**Human choice.** `PUT /conversations/{id}/category` is one unconditional core
`UPDATE ... SET category_id = :c, category_locked = true ... RETURNING`, so it
never depends on what the session loaded earlier and the response carries the
fresh row. A category deleted mid-request gives `404 category not found`.
Nothing sets the lock back to false. Deleting a category does not touch `category_locked`.

**The line's order.** `waiting_since` is set by `HandoffService.handoff` (a core
`UPDATE ... SET waiting_since = now()` that also clears any claim), which every
route into a handoff uses: the tool, the operator endpoint and sentiment.
`resume` and `close` set `waiting_since`, `claimed_by` and `claimed_at` to NULL.
`ConversationsRepository.list_waiting` returns the unclaimed `HUMAN_HANDOFF`
rows by `waiting_since` ascending (nulls last), then `id`; `GET /lines` groups
them by `category_id`. A line is a query, not a table, so there is nothing to
reorder. A move, by the model or a human, changes only `category_id`:
`waiting_since` is the time of the handoff, not of the move, so the conversation
appears in the new line at its original place and a move never sends anyone to
the back. A release likewise only clears the claim.

**The claim is one conditional update.** `claim` is
`UPDATE conversations SET claimed_by = :op, claimed_at = coalesce(claimed_at, now())
WHERE id, tenant_id, state = 'HUMAN_HANDOFF' AND (claimed_by IS NULL OR claimed_by = :op)
RETURNING`. Two operators racing: the database serializes the updates, one
matches and the other gets no row. The endpoint then reads the row to tell the
409s apart (not found, not waiting, already claimed). The same operator claiming
again succeeds and keeps the first `claimed_at`. `release` is
`UPDATE ... SET claimed_by = NULL, claimed_at = NULL WHERE claimed_by IS NOT NULL`.
The repository methods do not commit; the endpoint does. After a core `UPDATE`
the loaded object is stale until refreshed, so endpoints that return
`ConversationOut` refresh first.

**Limits.** A claim never expires, and `release` does not check who asks. The
operator id is not validated. See `docs/FOLLOW_UPS.md`.

## Paging, the poll and the indexes

**Keyset paging.** The helpers are in `app/api/pagination.py`. A paged route
takes `limit` (1 to 500, default 100) and `cursor`, asks the repository for
`limit + 1` rows, returns `limit` and, if the extra row came back, sets
`X-Next-Cursor`. The body stays a plain array, so old callers keep working. The
cursor is the url-safe base64 of a small JSON object holding the sort key of the
last row; a bad one is 422 `invalid cursor`. There is no `OFFSET`, so a page
costs the same however deep it is, and a row added or removed between pages does
not shift the others. Sort keys: `(created_at, id)` for agents, channels, tools
and contacts (`BaseRepository.list_page`); `(name, id)` for categories;
`last_message_at DESC NULLS LAST, created_at DESC, id DESC` for conversations
(`list_for_inbox`). A cursor timestamp without a timezone is 422. `GET /lines`
and the memories list are not paged.

**The poll runs on `seq`.** `GET /conversations/{id}/messages` is one `SELECT`
on `messages` (`MessagesRepository.page_by_conversation`) filtered in SQL:
`seq > :after_seq`, or `seq < :before_seq` when walking back, plus the tool-row
filter. `seq` comes from `messages_seq` (see "Message order"), so it is the real
insertion order and a client can keep one number between polls. `created_at`
would need a tie-break and comes from two clocks. The query is served by the
index `ix_messages_conversation_id_seq`, so the PRD's
`messages(conversation_id, created_at)` index was not added. Without a position
the endpoint returns the newest `limit` rows (oldest first) and the cursor is
`{"before_seq"}`; with one it returns the oldest `limit` rows after it and the
cursor is `{"after_seq"}`. The older `after=<timestamp>` filter still compares
`created_at`.

**Indexes (migration `0016`).** Built without `CONCURRENTLY`, so each blocks
writes to its table while it builds.

| Index | Serves |
|---|---|
| `ix_handoffs_conversation_id_created_at` | the newest handoff of a conversation (`latest_handoff` on `GET /conversations/{id}` and the inbox list) and the open handoff the handoff service looks up |
| `ix_memories_contact_id` | the memories of a contact, read on every turn and by `GET /contacts/{id}/memories` |
| `ix_conversations_tenant_state_last_message_at` | the operator inbox: `GET /conversations?state=...` for one tenant, newest activity first |

`agent_runs` already had its index (`agent_runs.conversation_id`).

## Inbound media

Voice notes and photos go through one channel-independent pipeline in core
— no channel or downstream stage (agent loop, humanizer, sentiment, summary,
barge-in) gets special-cased media behavior (PRODUCT.md).

1. **Adapter -> `IncomingMedia`**: each `ChannelAdapter.parse_incoming` maps
   its own payload into an `IncomingMedia` (bytes + mime + filename) on the
   `IncomingMessage`, exactly like it already maps text — the webchat
   adapter takes it from `POST /channels/webchat/media`'s multipart upload
   (`app/api/webchat.py`). The Evolution webhook downloads nothing: it stores
   the message with its type (`image`/`audio`) and no `media_key`, and strips
   `base64` and `jpegThumbnail` from the stored payload (`strip_base64`). The
   download happens in the turn (see "Where media is downloaded" below).
2. **Storage -> `Message.type`**: `app/messages/ingest.py` writes the file
   through `app.media.storage` (tenant-scoped local filesystem under
   `MEDIA_ROOT`, one `media_key` per file) and persists a `Message` with
   `type` `image` or `audio` (content empty except a webchat caption).
   Oversize uploads (bytes counted as received, no `Content-Length` needed) and content that sniffs as an unsupported type (`app/media/sniff.py`, not the client header) raise `MediaRejected` (413/415) before
   anything is written. Video, documents, stickers, locations, contacts get
   a persisted `unsupported` message instead of being dropped.
3. **Worker STT** (`app.media.prepare.prepare_media_messages`): before a
   debounced batch joins into the turn's input, every freshly-ingested audio
   message with empty `content` is transcribed (`app.media.stt.transcribe`,
   one OpenAI-compatible Whisper call) and its transcript becomes
   `Message.content` — idempotent by construction (a message with content
   already set, including a placeholder, is never re-transcribed), so
   downstream code (sentiment, rolling summary, the operator page) treats it
   exactly like a text message.
4. **`ContextBuilder` image parts**: image messages are handed to the LLM as
   multimodal content (image + the customer's caption as text) when
   `LLM_VISION_ENABLED` is on, capped at the newest `VISION_MAX_IMAGES` in
   the prompt window; older images and any image when vision is off/the
   model can't see it degrade to the text placeholder
   the text placeholder (setting `IMAGE_PLACEHOLDER`, default
   `"[imagem enviada pelo cliente]"`) so the turn still answers.
5. **Degradation**: STT failure (or empty transcript) keeps the message as
   `audio` with a fixed placeholder content (`"[áudio não compreendido]"` /
   `"[áudio sem fala detectada]"`; an audio still without content reaches the
   model as `AUDIO_PLACEHOLDER`) instead of costing the customer a reply.
   Unsupported types get a polite reply when a batch is unsupported-only: the
   agent's `unsupported_media_message`, else the default
   `app.conversations.turn.UNSUPPORTED_MEDIA_REPLY`; mixed batches (e.g. a photo + an unsupported sticker)
   run normally, with the unsupported item recorded as its own message.
6. **Rendering**: `/chat` and `/operator` render `type=="image"` as an
   `<img>` thumbnail (click to open full-size) plus any caption, and
   `type=="audio"` as an `<audio>` player plus the transcript text below.
   Both fetch `GET /media/{message_id}` (tenant-scoped, `X-API-Key`
   required) client-side and turn the response into an object URL, since
   `<img>`/`<audio>` tags can't set request headers.

**Where media is downloaded, and when it fails.** In the Evolution webhook the
order is: authenticate, parse, duplicate check (a repeated delivery answers
`{"status": "ok"}` and stops), rate limit, store. Media is downloaded by the
worker for every conversation state: `fetch_pending_media`
(`app/media/fetch.py`) is the first thing `process_turn` does, before the
returns for a conversation that is not `ACTIVE_AI` (an operator handling the
chat sees the photo and `GET /media/{id}` serves it; no AI reply goes out),
for an unavailable agent and for a refused endpoint (whose pending rows are
marked as failed downloads). Then come the owed-handoff check, the endpoint
check, `prepare_media_messages` (STT), the read receipt and the
unsupported-only reply. `fetch_pending_media` handles only `whatsapp_evolution`
channels and only image/audio rows without a `media_key`; it asks the turn's
lock check before each download (a lost lock: no download, the turn returns
`LockLost`); for each it ends the transaction, calls `EvolutionAdapter.fetch_media(external_message_id,
fallback_mime, channel)` (`POST {base_url}/chat/getBase64FromMediaMessage/{instance}`,
with the endpoint guard below), checks the bytes with `validate_media` (same
rules as `ingest`) and stores the file under the message's tenant. A download that
fails, is refused or is rejected by validation turns the row into `unsupported`
with content `[mídia não recebida]` and logs `media.fetch_failed` with the message
id only; the customer gets the unsupported-media reply, and the download is not
retried. The outcome (`media_key` or the failure mark) is committed at the end of
`fetch_pending_media`, so a rollback later in the turn neither downloads a row
again nor undoes a failure. A message rate-limited at the webhook is stored the same way and never
downloaded.

**Channel endpoint guard.** The Evolution `config.base_url` is tenant input,
so it gets the same address rules as tool endpoints
(`validate_channel_base_url` and `assert_safe_channel_url` in
`app/tools/ssrf.py`). `app/api/channels.py` runs the static check, without
DNS, on create and update (422 `invalid base_url: ...`).
`EvolutionAdapter._connection` runs the full check, with DNS, before every
outbound call (media download, send, read receipt, typing): a refusal logs
`channel.endpoint_refused` and raises `SSRFError`, and every client in the
adapter has `follow_redirects=False`. An operator reply through a refused
channel answers 502 `channel endpoint refused`. The one operator override is
`CHANNEL_ALLOWED_HOSTS` (`Settings.channel_allowed_hosts`): listed hosts skip
the address check, all others must still be public. `TOOL_ALLOWED_HOSTS` and
`TOOL_SSRF_DISABLED` do not apply to channels. The DNS-rebinding gap noted
for tools applies here too (see `docs/FOLLOW_UPS.md`).

**Capped media download.** `EvolutionAdapter.fetch_media` reads the response
in chunks and stops at `media_max_bytes * 4 // 3 + 65_536` raw bytes (base64
overhead plus headroom). A response with a non-identity `Content-Encoding`,
or decoded media over `MEDIA_MAX_BYTES`, is refused too. Each case raises
`MediaTooLarge`; the turn logs `media.fetch_failed`, turns the message into
`unsupported` with content `[mídia não recebida]` and writes no file.

Settings (`app/config.py`): `MEDIA_ROOT` (storage root, a shared Docker
volume between `api` and `worker` in compose), `LLM_VISION_ENABLED`,
`VISION_MAX_IMAGES`, `STT_BASE_URL`/`STT_API_KEY` (default to the LLM
provider's own base URL/key), `STT_MODEL`, `STT_LANGUAGE`,
`STT_TIMEOUT_SECONDS`.

Out of scope (PRODUCT.md): outbound media, video/documents/stickers beyond
the polite fallback, retention/cleanup, OCR as a separate step (vision reads
text in images), long-audio/streaming transcription.

## Operator UI

`app/static/operator.html` is a self-contained (no build, no framework)
static page served at `GET /operator`, giving a human the same handoff
surface `/chat` gives an end user — see PRODUCT.md "Operator UI".

- **Key gate**: on load the page reads the tenant `X-API-Key` from
  `localStorage["harness_operator_api_key"]`; if absent it shows a small
  form (prefilled with the demo key only when `/chat/bootstrap` answers,
  i.e. in demo mode) instead of any page content. Every
  fetch sends the stored key; a `401`/`403` from any call clears it and
  re-shows the gate — there is no separate login/session endpoint.
- **Inbox / Ativas**: two tabs over the same `GET /conversations` list —
  `state=HUMAN_HANDOFF` (with `include_handoff=true` for the brief) for the
  inbox, `state=ACTIVE_AI&state=WAITING` for Ativas — both polled every 3s,
  re-rendered only when the serialized JSON changes.
- **Conversation pane**: `GET /conversations/{id}/messages` polled every
  1.5s while a conversation is open; the inbox pane adds a reply box
  (`POST .../messages`), **Resumir** (`POST .../resume`) and **Fechar**
  (`POST .../close`); the Ativas pane is read-only plus one **Assumir**
  button (`POST .../handoff` with `requested_by=operator`) that moves the
  conversation into the inbox.
- **What it is not**: no operator accounts/login, no per-operator
  assignment, no notifications/unread counts, no run/tool/sentiment views —
  same "Not included" list as PRODUCT.md. Auth is exactly the existing
  tenant `X-API-Key`; the page adds no new auth code.

## Log context

Logs are structlog only; `app/observability/logging.py` is the one module that
imports stdlib `logging`. The processor `merge_contextvars` adds the bound keys
to every line. Four keys come from context variables:

| Key | Bound by | Unbound |
|---|---|---|
| `request_id` | `request_id_middleware`, per API request | `unbind_contextvars` in a `finally`, when the request ends |
| `conversation_id` | `process_turn`, at its start | `reset_contextvars` in its `finally` |
| `tenant_id` | `_run_turn`, once the conversation row has loaded | the same `reset_contextvars` |
| `run_id` | `AgentRuntime.run_turn`, after the run row's first commit | `reset_contextvars` in its own `finally` |

So every line logged while a turn runs has `conversation_id`, and `tenant_id`
from the moment the conversation loads. A line from the agent run has `run_id`
as well. A failure before the conversation loads has no `tenant_id`.

The binding functions return tokens, and the `finally` blocks restore by token
(`reset_contextvars`), never `clear_contextvars`. A key that was already bound
before the turn comes back with its old value; one that was not is removed.
The reset never raises, so it cannot hide the turn's outcome.

Tasks do not leak the context. The scheduler runs each turn as its own asyncio
task, and a task works on a copy of the context made when it is created: a key
bound inside one turn is invisible to the scheduler loop and to the other turns.
The `finally` blocks cover the case where a turn is run directly in the
caller's context, as the tests do.

`X-Request-ID` is kept only when the whole value matches
`^[A-Za-z0-9._-]{1,64}$`; otherwise the middleware generates a UUID. The
rejected value is neither logged nor echoed.

## Start order

`docker-compose.yml` brings the stack up in this order:

1. `postgres` and `redis`, each with a health check.
2. `migrate`, built from the same image. It waits for `postgres` to be healthy,
   runs `uv run alembic upgrade head` and exits 0 (`restart: "no"`).
3. `api` and `worker`. Each waits for `postgres` and `redis` (healthy), for
   `migrate` (`service_completed_successfully`) and for `mock-tools` (started).
   If `migrate` fails, neither starts.

The API does not migrate at start, so any number of replicas can start without
racing on the schema. The image runs as the user `app` (uid 10001); its
`/srv/media` belongs to `app`, and a volume created earlier by root keeps its
owner. `docker-compose.dev.yml` only adds the host ports of `postgres` and
`redis`.

## Checks

Three checks guard the code. `make check` runs them on a developer machine in
this order: `uv run ruff check .`, `uv run mypy`, then `uv run pytest -q`
(`make test` starts the Postgres container first when nothing listens on its
port). The workflow `.github/workflows/ci.yml` runs the same three on every
push, from a clean checkout with `uv sync --frozen`, against a Postgres 16
service.

mypy checks `app/` only. `app.security`, `app.queue` and `app.conversations`
are strict because they hold the code where a type mistake costs most: API key
and secret handling, the job queue, and the debounce, lock and turn logic that
decide who answers a customer and when. The rest of `app/` is checked loosely.
The strict flags are spelled out per module in `pyproject.toml` because
`strict = true` inside an override applied to every module in this mypy
version.

CI has no Redis service. The tests use `fakeredis`, so a Redis container would
add start-up time and nothing the tests use. `/ready` is tested with fakes that
fail on purpose (`tests/test_ready.py`).

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
  (`GET/POST /conversations/{id}/messages`, `/handoff`, `/resume`, `/close`,
  `GET /conversations`/`{id}`'s `latest_handoff` brief) is what `/operator`
  (see "Operator UI" below) is built on; further UI polish (accounts,
  assignment, notifications) is still out of scope per PRODUCT.md.
