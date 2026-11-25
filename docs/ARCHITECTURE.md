# Architecture

The map of the codebase for anyone changing it. How to run, configure and call
parley is in `README.md` and `docs/guides/` (see `llms.txt` for the index);
this document is about how the code works and why.

## Modules

```
app/
  api/            REST routers: tenants, agents, channels, tools, categories,
                   contacts, conversations, lines, webchat, media, webhooks,
                   health, operator, llms, plus chat and dev (mounted only when
                   `DEMO_MODE=true`; they 404 otherwise). A router parses and
                   validates, calls a repository or service, commits and returns
                   a response model. Auth dependencies are in
                   app/security/auth.py (X-Admin-Key, X-API-Key). operator.py
                   serves the static operator page with no auth of its own: the
                   page sends X-API-Key to the endpoints it calls.
  channels/       ChannelAdapter (parse_incoming, send_message; optional
                   check_endpoint, mark_read, send_typing) with webchat (REST in,
                   REST poll out) and evolution (WhatsApp). registry.py maps
                   ChannelType (`webchat`, `whatsapp_evolution`) to an adapter.
  messages/       ingest(): the one place inbound messages are deduplicated and
                   turned into a queue Job; every entry point calls it.
  conversations/  resolver (contact and conversation lookup or creation),
                   debounce (pending list, due ZSET, claim, in-flight list),
                   lock, retry (failed-turn counter, turn dead-letter list),
                   turn (process_turn: one batch -> one reply), summary (rolling
                   summary), humanize (reply splitting and pacing).
  agents/         context.py (ContextBuilder), runtime.py (AgentRuntime: the
                   tool-call loop, one AgentRun per turn), boundaries.py,
                   sentiment.py, categorize.py, loading.py (tenant-scoped lookup).
  llm/            base.py (provider-agnostic types), factory.py (Settings ->
                   provider), providers/ (openai_compatible.py is the only wire
                   format; fake.py is a scripted provider for tests and demos).
  tools/          base.py (ToolExecutor, ToolResult, ToolContext), registry.py,
                   builtin.py (remember, handoff_to_human), http.py (the tenant's
                   HTTP tools), ledger.py, ssrf.py, validation.py.
  media/          storage.py (local, tenant-scoped), sniff.py, stt.py, fetch.py
                   (worker download of WhatsApp media), prepare.py (audio -> text).
  handoff/        service.py (the only code that moves a conversation in or out
                   of HUMAN_HANDOFF), summarizer.py (the operator brief).
  memory/         MemoryService: long-term facts about a contact.
  security/       auth, secrets (Fernet), ratelimit (Redis counters), limits.
  queue/          Queue Protocol and RedisQueue.
  workers/        main.py (job loop, housekeeping loop, shutdown), scheduler.py.
  models/, repositories/   one model per table; BaseRepository takes tenant_id
                   as the first argument of every method.
  observability/  structlog configuration and the request-id middleware.
  config.py, db.py, http_client.py, http_retry.py, audit.py
```

Rate limits (`app/security/ratelimit.py`) are fixed one-minute windows in Redis,
per user and then tenant-wide; what callers see is in
`docs/guides/api-conventions.md`. A message the WhatsApp webhook rate-limits is
stored without a job, and its media is stored as `unsupported`, so it is never
downloaded.

`RedisQueue.fail` moves a job to the job dead-letter list `harness:dlq` after
`MAX_ATTEMPTS`, but nothing in `app/` calls it. Failed turns use their own list,
`harness:dlq:turns`.

## Data model

Every tenant-owned table inherits `TenantScoped` (`tenant_id` first, indexed).
Isolation is enforced by every repository and query filtering on `tenant_id`
explicitly; the column is the shape row-level security would key off.

```
tenants (admin-created; api_key_hash)
  └─ agents (instructions, model, provider, tool_names[], memory_enabled,
             handoff_enabled, handoff_message, max_tool_iterations,
             context_message_limit, summary_threshold,
             sentiment_escalation_enabled, boundary_*, error_message,
             unsupported_media_message, ...)
  └─ channels (type, agent_id, config, secret_encrypted)
       └─ contacts (external_user_id, unique per channel)
            └─ conversations (agent_id, channel_id, contact_id, state,
                               summary, summary_last_seq, last_message_at,
                               category_id?, category_locked, waiting_since?,
                               claimed_by?, claimed_at?)
                 ├─ messages (seq, direction, role, type, content, raw_payload,
                 │            media_key?, media_mime?, agent_run_id?,
                 │            tool_call_id?, external_message_id?)
                 ├─ agent_runs (status, input_text, input_message_ids,
                 │              output_text, llm_calls, tool_calls, tokens,
                 │              latency_ms, error, blocked_text,
                 │              boundary_reason)
                 │    └─ tool_executions (tool_id?, tool_name, arguments,
                 │                        result, status, model_content,
                 │                        terminal_text, latency_ms, error)
                 └─ handoffs (reason, requested_by, summary, resolved_at)
  └─ categories (name, description; unique per tenant)
  └─ tools (name, description, input_schema, endpoint, method,
            auth_config_encrypted, timeout_seconds; unique name per tenant)
  └─ memories (contact_id, type, content, importance, expires_at,
               last_accessed_at, meta) — facts about a contact, independent of
               any one conversation
```

**States.** `ConversationState` is `ACTIVE_AI`, `WAITING`, `HUMAN_HANDOFF` and
`CLOSED`. `HandoffService` allows `handoff` from `ACTIVE_AI` or `WAITING`,
`resume` only from `HUMAN_HANDOFF` (back to `ACTIVE_AI`), and `close` from any
state. Nothing sets `WAITING`. Closing is final for the row: the next customer
message opens a new conversation. `process_turn` runs the agent only when the
state is `ACTIVE_AI`; in any other state new messages are stored and no reply is
produced. The two exceptions (a handoff message still owed, and categorizing a
`HUMAN_HANDOFF` batch) are in "Queue / debounce algorithm", step 4.

**One open conversation per contact and channel.**
`uq_conversations_open_contact_channel` is a partial unique index on
`(tenant_id, contact_id, channel_id)` where `state != 'CLOSED'`.
`app/conversations/resolver.py` creates contacts and conversations with select,
`INSERT ... ON CONFLICT DO NOTHING`, select again (up to 3 tries), so two
simultaneous first messages share one contact and one conversation. The `0012`
migration closed existing duplicates first (keeping the one in `HUMAN_HANDOFF`,
else the most recently active); its downgrade only drops the index. It takes
`LOCK TABLE conversations IN SHARE ROW EXCLUSIVE MODE`, which blocks writes to
`conversations` while it runs.

**Message order.** A conversation is read in `Message.seq` order: a value from
the database sequence `messages_seq`, taken at insert (migration `0013`, which
numbers existing rows in one transaction under an `ACCESS EXCLUSIVE` lock on
`messages`). History, the rolling summary, the sentiment and category history,
the handoff brief, the scheduler's batch and `GET /conversations/{id}/messages`
all order by it. `created_at` is for display: inbound rows get it from the
database (`now()`), assistant, tool and operator rows from the app
(`datetime.now(UTC)`), so two machines with different clocks stamp a reply
earlier or later than the message it answers while `seq` stays in insertion
order. The only comparison on `created_at` is the `after=` filter of the
messages endpoint.

**Summary cursor and the row lock.** `conversation.summary_last_seq` is the
`seq` of the last message the rolling summary covered; the next summary takes
messages with a higher `seq`. A sequence value is taken before its row is
visible, so a transaction could commit a lower `seq` after the summary moved past
it, and that message would never be summarized. To prevent it, `ingest` (customer
messages) and `post_operator_message` (operator messages) lock the conversation
row (`SELECT ... FOR UPDATE`) before inserting and hold it until they commit, and
the summarizer takes the same lock before it reads the cursor and the pending
rows. The summarizer ends its transaction (`end_transaction`, which releases the
lock) before the model call and writes the new cursor afterwards. A turn's own
rows are committed before its summary runs, and one conversation has one turn at
a time. The operator message is sent first and the lock taken only to store it,
so the lock is never held across a send and a failed send stores nothing.

**The operator brief.** When a conversation is handed to a human, one best-effort
model call writes a short brief (who, what, why, tone; in the conversation's
language) and stores it in `handoffs.summary`. `handoff_with_summary`
(`app/handoff/summarizer.py`), used by the `handoff_to_human` tool and the
sentiment path, refuses an illegal state first, then generates the brief with no
transaction open, then makes the transition and sets the summary; the caller
commits. `POST /conversations/{id}/handoff` instead transitions and commits
first, so the AI is silenced whatever a running turn does, then generates the
brief and stores it with a second commit. The call reads the newest 30 messages
(tool results included), the contact's memories (when `memory_enabled`), the
rolling summary and the reason, and is bounded by the constant
`SUMMARY_TIMEOUT_SECONDS` (10) in that module. A model failure or timeout is
logged and swallowed: the handoff stands and `summary` stays null.
`GET /conversations/{id}` always attaches the newest handoff as `latest_handoff`;
the list endpoint and `GET /lines` do so only with `include_handoff=true`. The
brief never enters the message transcript, so the customer's poll never sees it.

## Queue / debounce algorithm

Goal: a burst of messages on one conversation becomes one agent turn, without
losing a message and without two workers handling the same conversation.
Everything that must be atomic is one Redis command or a `WATCH`/`MULTI`
transaction, never Lua: the tests run on fakeredis, which cannot `EVAL`.

1. `ingest()` persists the message, idempotent on
   `(tenant_id, channel_id, external_message_id)`, and pushes a `Job` onto
   `RedisQueue` (`harness:jobs`). Delivery is at-least-once.
2. The worker's job loop (`app/workers/main.py::run_once`) takes one job at a
   time. `dequeue` uses `BLMOVE` to move it from `harness:jobs` into the
   worker's own list `harness:processing:{worker_id}`, so a job is always in
   exactly one list. The loop then calls `debounce.push_pending`: `RPUSH` of the
   message id onto `harness:pending:{conversation_id}` and `ZADD GT` of
   `conversation_id -> now + DEBOUNCE_SECONDS` onto `harness:due`. `GT` only moves
   the score later, so the window slides while messages keep arriving and a
   message landing during a turn never makes the conversation due before the
   turn's claim ends. Only then is the job acked (removed from the processing
   list). Each worker refreshes `harness:worker:{id}` (TTL 30 s) and is listed in
   `harness:workers`; `housekeeping_loop` does this every 10 s and calls
   `reap_dead_workers`, which moves the jobs of a worker whose alive key has
   expired back onto `harness:jobs`, oldest first. A slow worker may be reaped and
   its job handled twice; `drain_pending` returns each message id once, so that
   is harmless. Both loops log their errors and carry on.
3. `run_scheduler` (`app/workers/scheduler.py`) keeps a pool of turn tasks, up to
   `WORKER_CONCURRENCY`. Every 250 ms it looks at conversations whose due score
   has elapsed and starts a turn for each it can claim while a slot is free,
   without waiting for turns already running. (`tick` is the run-to-completion
   variant most tests use; the worker does not call it.) Each turn is its own
   task with its own database session. `_take_turn` does, per conversation:
   - **claim**: `debounce.claim` takes `harness:claim:{id}` with `SET NX PX`
     (`CLAIM_TIMEOUT_SECONDS`, 60 s) and only the winner moves the due score to
     `now + 60 s`. The due entry is never removed by a claim, so a worker that
     dies from here on leaves the conversation due again when the score and the
     key run out;
   - **lock**: `harness:lock:{id}` (`SET NX PX`, 60 s, a random token; release
     and renew are compare-and-set on the token). If another worker holds it,
     `release_claim` re-arms the conversation one second out instead of
     dropping it. The same conversation never runs twice at once: claim plus
     lock;
   - **in-flight list**: `drain_pending` moves `pending` onto
     `harness:inflight:{id}` in one transaction and returns the whole in-flight
     list, each id once, so a batch a dead worker left behind comes back ahead
     of newer messages. The batch is loaded in `seq` order;
   - **heartbeat**: while the turn runs, a task renews the lock and extends the
     claim every 20 s (`LOCK_RENEW_INTERVAL_SECONDS`). Before every send and
     before the commits that store a turn's outcome, the turn asks whether the
     lock is still its own. Ownership is lost for good when a renewal finds
     another token or none, or when no renewal succeeded for a whole TTL. A
     turn that lost it rolls back, sends nothing more and returns `LockLost`;
     the scheduler then touches no debounce state, because whoever holds the
     lock now owns the batch (and if nobody does, the claim timeout brings it
     back). The one exception: a turn whose reply was already sent and stored
     takes exactly its message ids off the in-flight list (`drop_inflight`) so
     they are not answered twice;
   - **outcome**: after the heartbeat stops the scheduler renews once more to
     confirm the lock, then settles. A `Superseded` turn goes through
     `return_inflight`, which puts the in-flight batch back at the head of the
     pending list, ahead of what interrupted it (see "Barge-in"). A turn that
     answered or was refused on purpose clears the failure counter. A failed
     turn goes to `_settle_failure`;
   - **finish**: `debounce.finish`, under the lock, drops the in-flight list and
     the claim key and either removes the due entry or, if messages arrived on
     the pending list meanwhile (a returned superseded batch included), makes
     the conversation due now; the decision is atomic against `push_pending`.
     Then the lock is released. An error in the Redis bookkeeping itself is
     logged (`scheduler.turn_crashed`), skips `finish`, and leaves the batch in
     flight until the claim timeout.

   **Failures.** Any exception inside a turn rolls back what was not committed
   and surfaces as `TurnFailed(delivered_parts)`; a failed send of any bubble
   stops the reply there. `_settle_failure` counts it in
   `harness:turn_attempts:{id}` (expires an hour after the last failure). A turn
   that delivered something is dead-lettered at once (`partly_delivered` true):
   a retry would repeat what the customer saw. Otherwise, below
   `MAX_TURN_ATTEMPTS` (3) the batch is put back with `return_inflight` and the
   conversation is scheduled after `RETRY_BACKOFF_SECONDS` (2 s, then 8 s),
   counted from the failure. At the limit the entry goes to `harness:dlq:turns`,
   `finish` runs, and then `send_failure_notice` stores and sends the agent's
   `error_message` (else `TURN_FAILED_MESSAGE`) once, only for an `ACTIVE_AI`
   conversation; `finish` goes first so a crash cannot bring the batch back for a
   second notice. Operating these is in `docs/guides/operations.md`.

   **Shutdown.** On `SIGTERM`/`SIGINT` the job loop ends after its current
   `run_once` and the scheduler claims nothing new and waits for the running
   turns. `main` gives them `SHUTDOWN_GRACE_SECONDS` (90), then cancels the
   rest: those turns release their locks and their batches come back through the
   claim timeout. Last, the shared HTTP client and Redis are closed.
4. `process_turn` (`app/conversations/turn.py`) answers the batch. It is
   runtime-agnostic: it takes a text-in/text-out runtime (`EchoRuntime` is the
   default, so nothing calls a live model by accident) or a session-aware one
   (`AgentRuntime`, which writes its own rows). In order:
   1. An empty batch or unknown conversation returns `None`; the log context is
      bound (see "Log context").
   2. `fetch_pending_media` downloads WhatsApp media for every state (see
      "Inbound media"); a lost lock returns `LockLost`.
   3. A conversation in `HUMAN_HANDOFF` whose handoff message is still owed gets
      only that message, later. Any other state that is not `ACTIVE_AI` is
      skipped: a `HUMAN_HANDOFF` batch is categorized from the stored messages
      (best effort, no transcription) and gets no reply; `WAITING` and `CLOSED`
      are not categorized.
   4. The agent is loaded tenant-scoped (see "Tenant-scoped agent loading"); none
      means a refusal (`None`, nothing sent). The channel endpoint is checked
      against the SSRF rules (`ChannelAdapter.check_endpoint`) so a refused
      endpoint costs no model or tool call.
   5. The owed handoff message, if any, is delivered whole and the turn ends.
   6. `prepare_media_messages` transcribes audio. On a humanized channel the
      newest inbound message is marked read.
   7. A batch made only of `unsupported` media gets the agent's
      `unsupported_media_message` (else `UNSUPPORTED_MEDIA_REPLY`) with no model
      call. Mixed batches run normally.
   8. The batch is categorized, then, if `sentiment_escalation_enabled`, scored.
   9. The runtime runs. If the conversation is now `HUMAN_HANDOFF` (the tool
      handed off), the agent's `handoff_message` (`DEFAULT_HANDOFF_MESSAGE` when
      empty) is delivered whole, whatever text the runtime returned.
   10. Barge-in checkpoint 1, then delivery. The reply row is added only after
      its send succeeded (per bubble when humanized), so a failed send leaves no
      row; the reply is committed before the rolling summary, which is
      best-effort and can only drop itself. `last_message_at` is updated.

   A handoff is committed before its message is sent, so a failed send leaves the
   conversation in `HUMAN_HANDOFF` with the customer told nothing. The retry of
   that batch sends just the message: `_handoff_message_owed` is true when the
   conversation's newest handoff was requested by the agent or the system, is
   newer than the batch's newest message, and no assistant text was stored after
   it. The first comparison uses the database clock on both sides; the second
   compares a database timestamp with the app-stamped reply, so the database
   clock's offset is measured (`clock_timestamp()` between two local readings)
   and removed first. Handoffs requested by an operator or the user never owe a
   message.

## Agent loop

`AgentRuntime.run_turn` (a `SessionAwareTurnRuntime`, injected by the worker)
writes one `AgentRun` per turn, committed as `RUNNING` before the first model
call and committed again with its final status and output before it returns, so a
later rollback never leaves it `RUNNING`.

1. Build the prompt: `ContextBuilder` joins, as the system prompt,
   `agent.instructions`, the contact's long-term memories (when
   `memory_enabled`), the rolling summary, the humanizer style hint (when the
   channel has `humanize`) and the boundaries block; then the history. The
   history comes from `AgentRuntime._recent_messages`: `context_message_limit`
   (20 when unset) counts `USER`, `HUMAN_OPERATOR` and non-tool-call `ASSISTANT`
   rows, and the history is every row from the limit-th most recent counted one
   on, tool rows included. Tool calls are rebuilt from `Message.raw_payload`;
   tool results whose call is not in the window are dropped, and an operator
   message reads as `assistant`. `ContextBuilder.build` does not cut again.
2. Loop up to `max(1, agent.max_tool_iterations)` times:
   - call the `LLMProvider` with the transcript and the advertised `ToolSpec`s;
   - an empty answer is `SUCCESS` with a fixed "did not understand" text; a text
     answer is the final reply (`SUCCESS`), after the boundary check;
   - tool calls: persist a `tool_call` message, execute each through
     `ToolRegistry.execute`, persist a `tool_result` message per call and feed
     the results back. A successful result with `meta["terminal"]` (only
     `handoff_to_human` sets it) ends the loop at once with `meta["final_text"]`
     as the answer, skipping the extra model round trip.
3. Reaching the iteration cap is `AgentRunStatus.ERROR` with
   `error="max_iterations"` (no enum member of its own) and a fixed closing text;
   it does not hand off. An exception is caught the same way and the reply is the
   agent's `error_message`, else the default technical-problem text, so a broken
   turn still answers.

The turn has one deadline (`TURN_TIMEOUT_SECONDS`, 60) covering the whole run.
Only model calls run inside a timeout scope, for the time left; an expired
deadline raises before the next model step or tool call. Database work and tools
are never cancelled halfway (HTTP tools carry their own timeout), so a timed-out
turn leaves the session usable. The runtime exposes `llm_provider(agent)` so the
rolling summary uses the same, possibly scripted, provider as the turn.

### A turn is a series of short transactions

A turn does not hold a database transaction across slow work. Before every model
call (the reply check included), tool call, speech-to-text call, channel send,
paced wait and outbound endpoint check, the code calls
`app.db.end_transaction(session)`, which commits the open transaction if there is
one, so the connection goes back to the pool while the call runs. Sessions use
`expire_on_commit=False`, so loaded rows stay usable. The cost: what was written
before the call is committed and stays if the turn fails later. A model step's
tool messages are added to the session only after every call of the step
returned, so a tool that commits never leaves a `tool_call` row without its
results. Intermediate commits are not preceded by a lock check; the lock is
checked before every send and before the commits that store a turn's outcome.
Not every request path follows this: `ingest` writes an uploaded file to disk
while the request's transaction and the conversation row lock are open.

### The tool ledger

`app/tools/ledger.py` puts every tool call on record before it runs. `begin`
inserts a `ToolExecution` with status `STARTED` and commits it (with anything the
session had pending); `finish` fills in the result (`model_content` holds the
exact string the model received) and commits again, together with what the tool
wrote on the session (a built-in's `Memory` or `Handoff`). A row left `STARTED`
means the outcome is unknown. `HttpToolExecutor` also leaves it `STARTED`,
through `ledger.mark_unknown`, when the call failed after its request may have
reached the endpoint: an `httpx.TimeoutException` other than `ConnectTimeout`,
an `httpx.HTTPError` other than `ConnectError`, or an unexpected exception. A
blocked endpoint, `ConnectError`, `ConnectTimeout`, invalid arguments and any
HTTP response are finished normally. Either way the current attempt's model gets
the same failure text. A call to an unknown tool, or one not listed in the
agent's `tool_names`, writes no row. `finish` stores a terminal result's
`final_text` in `terminal_text`, and `replay` restores it, so a reused handoff
still ends the loop.

`ToolRegistry.execute` asks `ledger.find_prior` first. "Same turn" is decided by
the batch: `process_turn` passes the ids of the messages it answers to `run_turn`,
which stores them in `AgentRun.input_message_ids`. An earlier run counts when it
is in the same tenant and conversation and its ids are a non-empty subset of the
current run's: a retry has the same ids, a turn merged with newer messages a
superset, a later turn none of them. The match is on tool name and arguments, and
a finished row (the newest) wins over a `STARTED` one.

- finished (`OK` or `ERROR`): `ledger.replay` returns the saved result, the tool
  does not run and no row is written (`tools.result_reused`);
- `STARTED`: the call runs again only if the owning executor's
  `repeat_safe(name, ctx)` is true (an HTTP tool with GET; built-ins, whose
  effect and result are saved together). Otherwise the model gets
  `OUTCOME_UNKNOWN_ERROR` and the log gets `tools.outcome_unknown`. An executor
  without `repeat_safe` counts as not safe.

### The shared HTTP client

`app/http_client.py` holds one `httpx.AsyncClient` per process, created on first
use (`shared_client()`). The model provider, speech-to-text, the Evolution
adapter and the HTTP tools use it, each passing its own timeout per request;
redirects stay off. The API lifespan and the worker's `_close_connections` close
it through `close_shared_client()`; the provider's own `aclose` does not.
`app/http_retry.py` adds the short retry on 429/5xx for model calls,
speech-to-text and GET tools (`send_with_retry`); a GET tool repeats its SSRF
check before every try. Timeouts and connection errors are not retried at this
level.

### Tenant-scoped agent loading

Every place that needs a conversation's agent (`process_turn`, `run_turn`, the
rolling summary, the handoff brief and the categorizing of a `HUMAN_HANDOFF` batch) goes through
`load_conversation_agent` (`app/agents/loading.py`), which returns the agent only
when it belongs to the conversation's tenant. Otherwise it logs
`agent.tenant_mismatch` and returns `None`. Then `process_turn` logs
`turn.refused_agent_unavailable` and returns `None`: nothing is sent (not even a
read receipt), no `AgentRun` is written and nothing is committed. `run_turn`
raises `RuntimeError`, and the summary and the brief are skipped without a model
call. The API refuses to create such links; they can only exist in rows written
by an older version, which `python -m app.audit` lists (see
`docs/guides/operations.md`).

### Boundaries in a turn

Boundaries (what an agent may talk about; set up in
`docs/guides/agent-behaviour.md`) act at two points:

1. **Prompt**: `effective_boundaries` (`app/agents/boundaries.py`) merges the
   operator floor file with the agent's own boundaries, and `render_boundaries`
   writes them into the system prompt as a `## Boundaries` block.
2. **Check before delivery**: when the loop reaches a model-written final answer,
   `AgentRuntime._enforce_boundaries` runs `check_reply`, one extra model call
   counted in `llm_calls`. It is skipped when there are no boundaries or the
   check is off. A `BLOCK`, an error, a timeout or an unparseable answer swaps
   the reply for the refusal message, and the original goes to
   `agent_runs.blocked_text`. Canned messages and a tool's terminal text are not
   checked. Only the checked text, or the refusal, reaches the send path, so the
   humanizer and barge-in never see a blocked reply.

## Memory

Two kinds, deliberately different:

- **Short-term**: the tail of the persisted `messages` (see step 1 of "Agent
  loop") plus a rolling `conversation.summary`. `app/conversations/summary.py`
  advances it once the messages after `summary_last_seq` number at least
  `agent.summary_threshold` (every row counts, tool rows included; a threshold of
  0 or less turns it off). It is a model call that folds the new customer,
  assistant and operator messages into the prior summary. It never runs in the
  reply path: it happens after the reply is committed and never raises out of
  `process_turn`. A batch with nothing summarizable only advances the cursor.
- **Long-term**: `Memory` rows per contact, written by the agent through the
  `remember` built-in or through `POST /contacts/{id}/memories`.
  `MemoryService.get_contact_memories` returns non-expired rows ordered by
  `importance DESC, created_at DESC, id`, the first `MEMORY_CONTEXT_LIMIT` go into
  the system prompt each turn (when `agent.memory_enabled`), and reading writes
  nothing. `search()` is an `ILIKE` substring match. `MemoryService.store` is the
  one write path: it trims, collapses whitespace and lower-cases the content, and
  if a non-expired memory of the same contact has the same text it returns that
  row and raises its importance to the higher value instead of inserting.
  `last_accessed_at` is a column and a field of `MemoryOut`, but nothing writes
  it.

Memory is written only when the model calls `remember` or the API is called; no
background job extracts it.

## Tool model

Two sources merged by `ToolRegistry`, in priority order (the first executor whose
`can_handle` matches owns the name; `NoopExecutor` is the last resort and fails
with `unknown tool`):

1. **Built-in** (`app/tools/builtin.py`): `remember` and `handoff_to_human`,
   table-driven as `(ToolSpec, agent-flag)` pairs gated by the agent's
   `memory_enabled` and `handoff_enabled`; the flag controls both what is
   advertised and what runs. They write through the ledger like any tool.
2. **HTTP** (`app/tools/http.py`): the tenant's own `Tool` rows. `can_handle`
   claims every name (it has no session to know what exists), so it is registered
   last. A tool is callable only if its name is in the agent's `tool_names`.
   Arguments are validated against the tool's JSON Schema
   (`app/tools/validation.py`) before any request. Only GET is repeated on
   429/5xx; any other method is sent once. A response with a non-identity
   `Content-Encoding` fails as `unsupported response encoding` (requests send
   `Accept-Encoding: identity`), and a body past `TOOL_MAX_RESPONSE_BYTES` is cut
   and marked truncated. Every failure mode (invalid arguments, SSRF block,
   timeout, HTTP error, non-2xx) returns `ToolResult(ok=False, error=...)` and
   never raises into the runtime, so the model can read the error and apologize,
   retry or hand off.

   The SSRF guard (`app/tools/ssrf.py`) allows only globally routable addresses
   (IPv6 forms embedding an IPv4 address must embed a global one) and no
   redirects. At create or update time the endpoint is checked without DNS: it
   rejects `localhost` and `*.localhost`/`*.local`/`*.internal` names and
   non-global IP literals in any IPv4 notation; other host names are checked only
   at call time, when every resolved address must be global. `TOOL_ALLOWED_HOSTS`
   (authoritative when set) and `TOOL_SSRF_DISABLED` are in
   `docs/guides/configuration.md`. The check resolves once and `httpx` resolves
   again on connect, so a rebinding DNS record can still slip through; redirects
   are off to close the easier attack.

`ToolResult.meta` is also the terminal-tool mechanism: any executor can set
`{"terminal": True, "final_text": ...}` to end the loop; `handoff_to_human` is the
only user.

## Humanizer

Makes replies read as human, not bot-instant. Enabled per channel with
`config: {"humanize": true}`; `process_turn` then:

1. marks the newest inbound message read, through `ChannelAdapter.mark_read()`
   (WhatsApp only; a no-op by default), best effort;
2. asks the model, through the style hint in the system prompt, for short
   paragraphs, and splits the reply mechanically in `app/conversations/humanize.py`
   with no extra model call: on blank lines, falling back to sentence splitting
   when that yields a single part over ~280 characters, never inside a fenced code
   block, at most `HUMANIZE_MAX_PARTS` (overflow merges into the last);
3. paces delivery: before each part, `send_typing()` (a `sendPresence` call on
   Evolution) and a pause proportional to the part's length, clamped between the
   minimum and maximum delay. Each part is added and committed right after its
   send, so webchat polling shows them appear over seconds.

The knobs are in `docs/guides/configuration.md`. Without `humanize`, the reply is
sent as one message. The pauses happen inside the turn (lock held), so they slow
only that conversation. Failures of read and typing calls are logged and never
cost the reply.

## Barge-in

A message arriving mid-turn stops the stale reply instead of queuing a second
answer behind it. `process_turn` asks a `supersede_check` probe at exactly two
checkpoints, never mid-model or mid-tool: once after the runtime returns and
before anything is sent, and again between humanizer bubbles.

- **Sent bubbles stand.** A hit at checkpoint 2 drops the unsent parts; what
  went out stays out.
- **Observable, not an error.** The `AgentRun`s this turn created (found by id,
  not by time) become `SUPERSEDED` (an `ERROR` run keeps its status), the turn
  commits what the runtime wrote, skips `last_message_at` and the summary, and
  returns `Superseded(delivered_parts)` instead of a reply.
- **Requeue, one merged reply.** The scheduler's probe is
  `debounce.has_pending`, the existing pending list; on `Superseded` it calls
  `return_inflight`, and `finish` then makes the conversation due now, so the old
  batch and the new message are answered together.
- **A broken probe means not superseded.** `_is_superseded` never raises: a
  failed probe delivers the reply in full and the new message is answered by the
  next turn. The lock check has the opposite default (a check that raises counts
  as lost), because sending while another worker may be answering is the failure
  it prevents.
- **Handoff replies are exempt.** The handoff message (from the tool, the owed
  retry, or sentiment) is not a stale AI answer and goes out whole with no
  supersede checkpoint.

### State check before each send

An operator can hand off, resume or close a conversation while a turn runs.
`HandoffService` decides on the row read `FOR UPDATE`, never on the loaded object,
so a turn's own handoff after an operator's close is refused instead of reopening
the conversation; the operator handoff endpoint commits before it generates the
brief, so the operator never waits for the turn. Right before every send (the
single message and each bubble, after the lock check) `_state_change` reads the
conversation's state column from the database. If it differs from the state the
turn loaded, the turn rolls back what is uncommitted, logs
`turn.stopped_state_changed`, and returns the last delivered reply or `None`;
bubbles already sent stay, the rest is dropped, and no summary follows. A tool
call in flight is not interrupted. The three checks answer different questions:
the lock check, whether this worker still owns the conversation; the supersede
check, whether a newer customer message arrived (and requeues the batch); the
state check, whether someone changed the conversation's state.

## Sentiment escalation

Hands the conversation to a human when the customer gets angry, without them
asking. Enabled per agent with `sentiment_escalation_enabled` (default off; the
`/chat` demo agent has it on).

1. **Classifier** (`app/agents/sentiment.py`): one model call that scores the
   batch plus the 10 messages before it for frustration, 0.0 to 1.0. The batch
   rows are left out of the history because `input_text` already carries them.
   It runs in step 8 of `process_turn`, before the runtime, so a frustrated batch
   gets no AI answer.
2. **Threshold**: a score at or above `SENTIMENT_THRESHOLD` (0.7) escalates;
   `SENTIMENT_TIMEOUT_SECONDS` bounds the call. Both are global.
3. **Trip** (`_maybe_escalate_on_sentiment`): the agent runtime and the rolling
   summary are skipped; the conversation goes to `HUMAN_HANDOFF` through
   `handoff_with_summary` with `requested_by=system` and a reason that cites the
   score, and the agent's `handoff_message` is delivered through the normal send
   path (humanizer pacing applies). If an operator changed the state while the
   classifier or the brief ran, the handoff is refused and the turn ends with
   nothing sent.
4. **Best effort**: any error, timeout or unparseable answer scores `None`, which
   means no signal and the normal turn proceeds. Only `ACTIVE_AI` conversations
   are scored, so a conversation cannot escalate twice.

## Categories and waiting lines

parley stores the facts: a conversation's category, since when it waits and who
claimed it. Operators, line visibility and priorities belong to the app built on
top.

**Columns.** Migration `0014`: `categories` (unique `(tenant_id, name)`),
`conversations.category_id` (nullable, foreign key `ON DELETE SET NULL`, indexed)
and `category_locked` (default false). Migration `0015`: `waiting_since`,
`claimed_by` (text), `claimed_at`, and the partial index `ix_conversations_waiting`
on `(tenant_id, waiting_since)` where `state = 'HUMAN_HANDOFF'`; it backfilled
`waiting_since` for conversations already in `HUMAN_HANDOFF` from their newest
unresolved handoff.

**The classifier in the turn** (`app/agents/categorize.py`). One model call per
batch, modelled on the sentiment classifier. In step 8 of `process_turn` it runs
before the sentiment check, so an escalated batch is already filed when it is
handed off; it also runs in the `HUMAN_HANDOFF` skip branch (step 3), so the
category follows the subject while a human has the conversation. The model gets
the categories as a numbered list of `name: description` and answers a number,
`0` for none; only a valid new number is written. It is best effort and never
raises: no model call for a locked conversation or a tenant with no categories;
the reads run in a savepoint (a missing table does not fail the turn); no
transaction is open during the call; a timeout (`CATEGORY_TIMEOUT_SECONDS`),
provider error, bad answer or a category deleted meanwhile leaves the row
unchanged. The write is one `UPDATE ... WHERE category_locked IS FALSE` in a
savepoint, so a human's choice made during the call is never overwritten (nothing
is written and nothing logged in that case). The call is not counted in an
`AgentRun`'s `llm_calls`.

**Human choice.** `PUT /conversations/{id}/category` is one unconditional core
`UPDATE ... SET category_id = :c, category_locked = true ... RETURNING`, so it
does not depend on what the session loaded and the response carries the fresh
row. A category deleted mid-request gives `404 category not found`. Nothing sets
the lock back to false, and deleting a category does not touch it.

**The line's order.** `waiting_since` is set by `HandoffService.handoff` (a core
`UPDATE ... SET waiting_since = now()` that also clears any claim), which every
route into a handoff uses: the tool, the operator endpoint and sentiment. `resume`
and `close` set `waiting_since`, `claimed_by` and `claimed_at` to NULL.
`ConversationsRepository.list_waiting` returns unclaimed `HUMAN_HANDOFF` rows by
`waiting_since` ascending (nulls last), then `id`; `GET /lines` groups them by
`category_id`, every category of the tenant by name plus the uncategorized line,
and `limit` caps the total across lines, keeping the oldest. A line is a query,
not a table, so nothing needs reordering. A move, by the model or a human,
changes only `category_id`: the conversation keeps its original place in the new
line. A release likewise only clears the claim.

**The claim is one conditional update.** `claim` is
`UPDATE conversations SET claimed_by = :op, claimed_at = coalesce(claimed_at, now())
WHERE id, tenant_id, state = 'HUMAN_HANDOFF' AND (claimed_by IS NULL OR claimed_by = :op)
RETURNING`. Two operators racing: the database serializes the updates, one matches
and the other gets no row; the endpoint then reads the row to tell the 409s apart
(not found, not waiting, already claimed). The same operator claiming again
succeeds and keeps the first `claimed_at`. `release` is
`UPDATE ... SET claimed_by = NULL, claimed_at = NULL WHERE claimed_by IS NOT NULL`.
The repository methods do not commit; the endpoint does. After a core `UPDATE` the
loaded object is stale until refreshed, so endpoints that return `ConversationOut`
refresh first.

**Limits.** A claim never expires, `release` does not check who asks, and the
operator id is stored as given. An operator reply needs no claim and stores no
operator id. See `docs/FOLLOW_UPS.md`.

## Events

An `external` channel is a project's own inbox. `ExternalAdapter.send_message`
sends nothing: what the agent does reaches the project as events, in two ways,
a push and a feed.

**Outbox.** `record_event` (`app/events/outbox.py`) adds a `ChannelEvent` row to
the caller's session, so the event is committed with the change it describes
(the reply row, the state change in `HandoffService`, the category update). It
does nothing for a channel that is not `external`. `seq` is a global identity
column. Inserts of one channel take a transaction-scoped advisory lock keyed on
the channel, so within a channel insert order is commit order and a reader of
`seq > after` never skips a late commit.

**Push.** `deliver_due` (`app/events/delivery.py`), run by `event_push_loop` in
the worker, takes each channel's oldest unsent event (no `delivered_at`, no
`gave_up_at`) and POSTs it to `config.events_url`, signed
(`app/events/signing.py`). One event per channel is in flight: a failure moves
`next_attempt_at` along `BACKOFF_SECONDS` and the later events wait. The
`EVENT_PUSH_MAX_ATTEMPTS`th failure sets `gave_up_at`; the channel then goes on
with the next event. A claim leases the event (`next_attempt_at` moved ahead,
`FOR UPDATE SKIP LOCKED`) so two workers do not push it together, and no lock
is held across the HTTP call. The URL is checked on every push like an
Evolution `base_url`; redirects are not followed.

**Feed.** `GET /channels/{channel_id}/events` (`app/api/external.py`) reads the
same rows by `seq`, whatever their delivery state. Pushes are at least once, so
the receiver dedupes on the event id. Staff messages and hand back go through
`HandoffService` like an operator's, which is where `conversation.handoff` and
`conversation.agent` are recorded.

## Paging, the poll and the indexes

How callers page is in `docs/guides/api-conventions.md`.

**Keyset paging.** Helpers are in `app/api/pagination.py`. A paged route asks the
repository for `limit + 1` rows, returns `limit` and, if the extra row came back,
sets `X-Next-Cursor`; the body stays a plain array. The cursor is the url-safe
base64 of a small JSON object holding the sort key of the last row. There is no
`OFFSET`, so a page costs the same however deep it is and a row added or removed
between pages does not shift the others. Sort keys: `(created_at, id)` for
agents, channels, tools and contacts (`BaseRepository.list_page`); `(name, id)` for
categories; `last_message_at DESC NULLS LAST, created_at DESC, id DESC` for
conversations (`list_for_inbox`). A cursor timestamp without a timezone is 422.
The cursor holds only the sort key, so changing a filter between pages is not
detected. `GET /lines` and the memories list are not paged.

**The poll runs on `seq`.** `GET /conversations/{id}/messages` is one `SELECT`
(`MessagesRepository.page_by_conversation`) filtered in SQL: `seq > :after_seq`, or
`seq < :before_seq` when walking back, plus the tool-row filter (tool rows only
with `include_tools`). `seq` is the real insertion order (see "Message order"), so
a client keeps one number between polls, which a timestamp from two clocks cannot
give. The index `ix_messages_conversation_id_seq` serves it. Without a position the
endpoint returns the newest `limit` rows, oldest first, and the cursor is
`{"before_seq"}`; with one it returns the oldest `limit` rows after it and the
cursor is `{"after_seq"}`. The `after=<timestamp>` filter still compares
`created_at`.

**Lookup indexes (migration `0016`).** Built without `CONCURRENTLY`, so each blocks
writes to its table while it builds.

| Index | Serves |
|---|---|
| `ix_handoffs_conversation_id_created_at` | the newest handoff of a conversation (`latest_handoff`) and the open handoff the handoff service looks up |
| `ix_memories_contact_id` | the memories of a contact, read on every turn and by `GET /contacts/{id}/memories` |
| `ix_conversations_tenant_state_last_message_at` | the operator inbox: `GET /conversations?state=...`, newest activity first |

`agent_runs.conversation_id` and `tool_executions.agent_run_id`, which the ledger's
`find_prior` filters and joins on, are indexed.

## Inbound media

Voice notes and photos go through one channel-independent pipeline: no channel or
later stage (agent loop, humanizer, sentiment, summary, barge-in) has media-specific
behavior of its own.

1. **Adapter -> `IncomingMedia`**: `parse_incoming` maps a payload to an
   `IncomingMedia` (bytes, mime, filename) on the `IncomingMessage`; the webchat
   adapter gets it from `POST /channels/webchat/media`'s multipart upload. The
   Evolution webhook downloads nothing: it stores the message with its type
   (`image`/`audio`) and no `media_key`, and strips `base64` and `jpegThumbnail`
   from the stored payload (`strip_base64`).
2. **Storage -> `Message.type`**: `ingest` validates (`validate_media`) and writes
   the file through `app.media.storage` (local filesystem under `MEDIA_ROOT`, key
   `{tenant_id}/{uuid}{ext}`) and persists a message of type `image` or `audio`
   (content empty except a webchat caption). The declared mime must be allowed for
   the type, the size within `MEDIA_MAX_BYTES` (counted as bytes arrive, so no
   `Content-Length` is needed), and the content, sniffed from magic bytes
   (`app/media/sniff.py`) and not from the client header, must match the type;
   the sniffed mime is what is stored. Otherwise `MediaRejected` (413/415) before
   anything is written, and a duplicate delivery is detected before the file is
   written. Video, documents, stickers, locations and contacts are stored as an
   `unsupported` message instead of being dropped.
3. **Download (WhatsApp)**: see "Where media is downloaded" below.
4. **Speech-to-text**: `prepare_media_messages` transcribes every audio message
   that has a file and empty `content` (`app.media.stt.transcribe`, one
   OpenAI-compatible Whisper call) and writes the transcript into `Message.content`,
   so sentiment, summary and the operator page treat it as text. It is idempotent:
   a message with content, a failure placeholder included, is never transcribed
   again.
5. **Image parts**: `ContextBuilder` hands image messages to the model as
   multimodal content (image plus the caption) when `LLM_VISION_ENABLED`, for the
   newest `VISION_MAX_IMAGES` in the history window. Older images, any image when
   vision is off, and an image whose file cannot be read become the text
   placeholder (`IMAGE_PLACEHOLDER`, with the caption when there is one).
6. **Degradation**: a failed or empty transcription keeps the message as `audio`
   with the content `[áudio não compreendido]` or `[áudio sem fala detectada]`,
   and an audio still without content reaches the model as `AUDIO_PLACEHOLDER`, so
   the customer still gets a reply. A batch made only of `unsupported` media gets
   the fixed reply (step 7 of `process_turn`).
7. **Rendering**: `/chat` and `/operator` show images as `<img>` and audio as an
   `<audio>` player with the transcript below. Both fetch `GET /media/{message_id}`
   (tenant-scoped, `X-API-Key`) client-side and use an object URL, since those tags
   cannot send headers.

**Where media is downloaded, and when it fails.** In the Evolution webhook the
order is: authenticate, parse, duplicate check (a repeated delivery answers
`{"status": "ok"}` and stops), rate limit, store. The worker downloads the media,
for every conversation state: `fetch_pending_media` (`app/media/fetch.py`) is the
first thing `process_turn` does, before any early return, so an operator handling
the chat sees the photo even when no AI reply goes out. It handles only
`whatsapp_evolution` channels and only image/audio rows without a `media_key`. It
asks the turn's lock check before each download (a lost lock stops: no download,
`LockLost`); for each row it ends the transaction, calls
`EvolutionAdapter.fetch_media` (`POST {base_url}/chat/getBase64FromMediaMessage/{instance}`),
checks the bytes with `validate_media`, the same rules as `ingest`, and stores the
file under the message's tenant. A download that fails, is refused by the endpoint
guard or fails validation turns the row into `unsupported` with content
`[mídia não recebida]` and logs `media.fetch_failed` with the message id only; the
customer gets the unsupported-media reply and the download is not retried. The
outcome is committed at the end of `fetch_pending_media`, so a rollback later in
the turn neither downloads a row again nor undoes a failure. Inline base64 sent by
Evolution is stripped, never used.

**Capped download.** `EvolutionAdapter.fetch_media` reads the response in chunks and
stops at `media_max_bytes * 4 // 3 + 65_536` raw bytes (base64 overhead plus
headroom), asks for `Accept-Encoding: identity` and refuses any other
`Content-Encoding`, under an overall deadline; decoded media over `MEDIA_MAX_BYTES`
is refused too. Each case raises `MediaTooLarge` and ends as the failure above, with
no file written.

**Channel endpoint guard.** The Evolution `config.base_url` is tenant input, so it
gets the same address rules as tool endpoints (`validate_channel_base_url`,
`assert_safe_channel_url` in `app/tools/ssrf.py`). `app/api/channels.py` runs the
static check, without DNS, on create and update (422 `invalid base_url: ...`).
`EvolutionAdapter._connection` runs the full check, with DNS, before every outbound
call (media download, send, read receipt, typing): a refusal logs
`channel.endpoint_refused` and raises `SSRFError`, and `process_turn` runs the same
check up front. An operator reply through a refused channel answers 502
`channel endpoint refused`. The one override is `CHANNEL_ALLOWED_HOSTS`; the tool
settings do not apply to channels, and the DNS-rebinding gap of tools applies here
too. Media settings are in `docs/guides/configuration.md`. Outbound media,
retention and cleanup, remote storage and a separate OCR step do not exist.

## Operator UI

`app/static/operator.html` is a self-contained page (no build, no framework) served
at `GET /operator`. It is a thin demo client of the same REST surface an operator
app would use; usage of those endpoints is in `docs/guides/operators.md`.

- **Key gate**: on load the page reads the tenant `X-API-Key` from
  `localStorage["harness_operator_api_key"]`; without one it shows a form (prefilled
  only when `/chat/bootstrap` answers, i.e. in demo mode). Every fetch sends the
  key, and a `401`/`403` clears it and shows the gate again. There is no login or
  session endpoint. The operator id is a free-text field kept in
  `localStorage["harness_operator_id"]`.
- **Tabs**: **Filas** shows `GET /lines?include_handoff=true`, grouped by category,
  each row with an **Atender** (claim) button; **Minhas** shows
  `GET /conversations?state=HUMAN_HANDOFF&claimed_by=<operator id>`; **Ativas**
  shows `state=ACTIVE_AI&state=WAITING`. The list is polled every 3 s and
  re-rendered only when the JSON changes; wait times refresh every 30 s.
- **Conversation pane**: `GET /conversations/{id}/messages` is polled every 1.5 s
  and loads the latest page each time (the page does not use `after_seq`). For a
  conversation the current operator claimed, the pane has a reply box
  (`POST .../messages`), **Resumir** (`/resume`), **Fechar** (`/close`) and a
  release button (`/release`); one claimed by someone else shows who has it, and an
  unclaimed one an **Atender** button. Every handoff has a category selector
  (`PUT .../category`). An active conversation has **Assumir**, which calls
  `POST .../handoff` and then claims it. That only the claimant can reply is the
  page's rule; the API does not require a claim.
- **Not there**: operator accounts, notifications, unread counters, category
  administration, and run, tool or sentiment views.

## Log context

Logs are structlog only; `app/observability/logging.py` is the one module that
imports stdlib `logging`. The processor `merge_contextvars` adds the bound keys to
every line. Four keys come from context variables:

| Key | Bound by | Unbound |
|---|---|---|
| `request_id` | `request_id_middleware`, per API request | `unbind_contextvars` in a `finally`, when the request ends |
| `conversation_id` | `process_turn`, at its start | `reset_contextvars` in its `finally` |
| `tenant_id` | `_run_turn`, once the conversation row has loaded | the same `reset_contextvars` |
| `run_id` | `AgentRuntime.run_turn`, after the run row's first commit | `reset_contextvars` in its own `finally` |

Every line logged while a turn runs has `conversation_id`, and `tenant_id` from the
moment the conversation loads; a line from the agent run has `run_id` too. A failure
before the conversation loads has no `tenant_id`.

The binding functions return tokens and the `finally` blocks restore by token
(`reset_contextvars`), never `clear_contextvars`: a key already bound before the turn
comes back with its old value, one that was not is removed. The reset never raises,
so it cannot hide the turn's outcome. The scheduler runs each turn as its own
asyncio task, which works on a copy of the context made when it is created, so a key
bound inside one turn is invisible to the scheduler loop and to other turns; the
`finally` blocks cover a turn run directly in the caller's context, as tests do.

`X-Request-ID` is kept only when the whole value matches `^[A-Za-z0-9._-]{1,64}$`;
otherwise the middleware generates a UUID. The rejected value is neither logged nor
echoed.

## Start order

`docker-compose.yml` starts `postgres` and `redis` (health-checked), then `migrate`
(the same image, `uv run alembic upgrade head`, `restart: "no"`), then `api` and
`worker`, each waiting for `postgres` and `redis` (healthy), `migrate`
(`service_completed_successfully`) and `mock-tools` (started). If `migrate` fails
neither starts. The API does not migrate at start, so any number of replicas can
start without racing on the schema. `docker-compose.dev.yml` only adds the host
ports of `postgres` and `redis`; `docker-compose.stub.yml` adds a stub model
server. Stack notes (replicas, the non-root image user and media volume) are in
`docs/guides/operations.md`.

## Checks

`make check` runs `uv run ruff check .`, `uv run mypy` and `uv run pytest -q`, in
that order; `make test` starts the Postgres container first when nothing listens on
its port. `.github/workflows/ci.yml` runs the same three on every push against a
Postgres 16 service, with no Redis service: the tests use `fakeredis`. `/ready`
(database and Redis only) is tested with fakes that fail on purpose
(`tests/test_ready.py`).

mypy checks `app/` only. `app.security`, `app.queue` and `app.conversations` are
strict because a type mistake costs most there: API key and secret handling, the
job queue, and the debounce, lock and turn logic that decide who answers a customer
and when. The strict flags are spelled out per module in `pyproject.toml` because
`strict = true` inside an override applied to every module in this mypy version.

## Extension points

- **New channel**: implement `ChannelAdapter` (`app/channels/base.py`), register it in
  `app/channels/registry.py` and add a webhook route if it is push-based.
- **New LLM provider**: `app/llm/factory.py::get_provider` is the one dispatch point;
  a new branch there and a class implementing `LLMProvider.complete` is the whole
  change.
- **New built-in tool**: add a `(ToolSpec, agent-flag)` pair to `BUILTIN_TOOLS` and a
  handler to `_HANDLERS` in `app/tools/builtin.py`; no registry or runtime change.
- **Semantic memory**: `MemoryService.search` is the seam: swap the `ILIKE` for a
  vector-similarity query without touching `app/tools/builtin.py` or
  `app/agents/runtime.py`.
