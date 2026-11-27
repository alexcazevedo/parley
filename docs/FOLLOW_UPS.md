# Follow-ups

Known gaps and next candidates, collected across comb runs. Each is a
plausible starting point for a new idea. Not ordered by priority.

## Open now

What is still open, one line per item, grouped by topic, each pointing to the
section that holds its full text. The sections below keep their history, done
markers included. "(verify)" marks an item that may have been fixed or may no
longer apply; check the code before relying on it.

**Security and channels**
- End-user-scoped webchat token (From the security run).
- Tool endpoint check accepts trailing-dot hosts at create time (From the security run).
- SSRF DNS-rebinding TOCTOU (tools and channels) (Older known MVP limitations (README)).
- Default database credentials remain (From the ops run).
- Real auth: static API keys only (Product-surface gaps left out of scope by recent runs).
- Calls to an MCP server and to HTTP tools are not signed (From the business tools run).
- A person under an `@lid` and a phone number is two contacts (From the conversation-correctness run).

**Boundaries**
- Inbound screening (From the boundaries run).
- One retry before refusing (From the boundaries run).
- Handoff after repeated blocks (From the boundaries run).
- Tenant-wide boundaries (From the boundaries run).
- Operator page screens for boundaries (From the boundaries run).
- Measure block and false-block rates on a real model (From the boundaries run).
- A pre-check job: judge the customer's message before the agent answers (From the model per job run).
- Comparing sentiment and department on a candidate model, as the boundary check can be (From the model per job run).
- Typed answers on a chat model: done for `llm:`, `openai:`, `anthropic:` and `google:` jobs; an unset job still answers in text (From the native providers run).
- Retire the hand-written `openai_compatible` provider (From the native providers run).
- Credentials per tenant for a native provider (From the native providers run).
- Prompt caching, with measurements (From the native providers run).
- Settings per agent or per tenant for a job's model; today one setting for the server (From the model per job run).

**Memory**
- Facts that expire (From the session memory run).
- Searching old messages of earlier sessions (From the session memory run).
- Per-customer session settings (From the session memory run).
- Vendezap closing the conversation on a finished pedido (From the session memory run).

**Turns, delivery and recovery**
- Resend only the missing bubbles (From the reliability run).
- API or page for the dead-letter list, and replay (From the reliability run).
- A failed turn that lost its lock can be answered again (From the reliability run).
- A crash while dead-lettering can double the entry (From the reliability run).
- Test seams in the scheduler (`_pause`, `_exclusive_sessions`) (From the reliability run).
- The production scheduler path is thinly tested (From the reliability run).
- Rows written before a slow call stay when the turn later fails (From the reliability run, part 2).
- The intermediate commits are not preceded by a lock check (From the reliability run, part 2).
- A reply row is stored after its send (From the reliability run, part 2).
- A handoff whose message never goes out (From the reliability run, part 2).
- A reused lookup result can be seconds old (From the reliability run, part 2).
- Unknown-outcome calls are visible only in logs and `tool_executions` (From the reliability run, part 2).
- API request paths still hold a transaction during media fetch (verify) (From the reliability run, part 2).
- Transport errors are not retried (From the reliability run, part 2).
- A retried turn's prompt contains the earlier attempt's saved tool messages (From the reliability run, part 2).
- One unexplained failure of `tests/test_e2e_demo.py` (verify) (From the reliability run, part 2).
- Tested with fakeredis and `respx` only (From the reliability run, part 2).
- A tool call in flight completes after an operator takes over (From the conversation-correctness run).
- A cut-off reply is not explained to the customer (From the conversation-correctness run).
- A turn stopped by an operator is recorded only as a log line (From the conversation-correctness run).
- `max_iterations` as a real `AgentRunStatus` member (Older known MVP limitations (README)).

**Migrations and stored data**
- Migration `0012` closes duplicates and its downgrade does not reopen them (From the conversation-correctness run).
- Migration `0012` blocks writes to `conversations` while it runs (From the conversation-correctness run).
- Migration `0013` rewrites every message row under an exclusive lock (From the conversation-correctness run; also From the conversation-correctness run, part 2).
- Indexes were built without `CONCURRENTLY` (migration `0016`) (From the API surface run).
- Base64 remains in payloads stored before the cleanup (From the conversation-correctness run, part 2).
- Stored rows are not checked against the new bounds (From the API surface run).
- Names still say `harness` (Redis keys, database, compose defaults, browser storage key) (From the ops run).

**Media**
- Order-ID normalization for real tools (From the inbound-media run (2026-08-25)).
- Outbound media (From the inbound-media run (2026-08-25)).
- Media retention/cleanup (From the inbound-media run (2026-08-25)).
- Remote storage (From the inbound-media run (2026-08-25)).
- Live Evolution media probe (verify) (From the inbound-media run (2026-08-25)).
- A failed media download is not retried (From the conversation-correctness run, part 2).
- Inline base64 sent by Evolution is not used (From the conversation-correctness run, part 2).
- Transcription language is per deployment (From the ops run).
- The default texts are Portuguese only (From the ops run).
- An existing media volume needs one manual `chown` (From the ops run).

**Context, memory and categories**
- No cap on the size of the prompt (From the conversation-correctness run, part 2).
- `created_at` still comes from two clocks (From the conversation-correctness run, part 2).
- Semantic long-term memory (Older known MVP limitations (README)).
- Background memory extraction (Older known MVP limitations (README)).
- A duplicate memory is found by exact text only (From the API surface run).
- `last_accessed_at` is unused (From the API surface run).
- One more model call per batch (categories) (From the categories run).
- No unlock of a human's category choice (From the categories run).
- Claims never expire and the releaser is not checked (From the categories run).
- No priorities, counts or reports (From the categories run).
- No audio transcription for categorization during a handoff (From the categories run).
- A human's choice during a model call is dropped silently (From the categories run).
- `GET /lines` caps the total, not each line (From the categories run).
- A conversation that is not handed off has no wait time (From the categories run).

**API surface**
- Pick single tools of an MCP server per agent (From the business tools run).
- `GET /lines` and the memories list are not paged (From the API surface run).
- No total counts (From the API surface run).
- Lists return at most 100 rows by default (From the API surface run).
- Pass the same filters with a cursor (From the API surface run).
- The demo pages do not use `after_seq` (From the API surface run).
- REST-poll only (no WebSocket/SSE) (Older known MVP limitations (README)).

**Operator page**
- No category admin on the demo page (From the categories run).
- No tab counters, and no automated browser test of the click flows (From the categories run).
- Accounts/roles/assignment, notifications, unread counters, supervision view (Product-surface gaps left out of scope by recent runs).

**CI, tests and operations**
- No coverage measurement (From the CI run).
- Tests are not type-checked (From the CI run).
- The rest of `app/` is not strict (From the CI run).
- Two `type: ignore` remain in `app/` (From the CI run).
- CI does not build the Docker image (From the CI run; also From the ops run).
- No real Redis in tests (From the CI run).
- Branch protection is not set (verify) (From the CI run).
- The workflow has never run on GitHub (verify) (From the CI run).
- `/ready` does not check the worker or the model endpoint (From the CI run).
- `make db` ignores a `POSTGRES_PORT` set only in `.env` (From the CI run).
- Per-tenant LLM keys and base URL, token and cost accounting, metrics (From the ops run).
- Leftover wording that names tickets in comments (From the ops run).
- No test starts the stack (From the ops run).
- Sentiment per-tenant/channel threshold, cooldown, score persistence (Product-surface gaps left out of scope by recent runs).
- Humanizer per-channel pacing settings (Product-surface gaps left out of scope by recent runs).
- Barge-in: "user is typing" pause; retracting sent bubbles (Product-surface gaps left out of scope by recent runs).
- Metrics/tracing and a deploy setup (Product-surface gaps left out of scope by recent runs).

## From the security run

- **End-user-scoped webchat token (PRD E2.6).** The built-in `/chat`
  widget keeps the tenant API key in the browser, so it is demo-only
  (`DEMO_MODE=true`). A real browser widget needs a short-lived token
  scoped to one end user/contact and channel (minted server-side with the
  tenant key), accepted only by the webchat message/media/poll routes.
- **Tool endpoint check accepts trailing-dot hosts at create time
  (`http://10.0.0.5.`).** The call-time check refuses them. Channels are
  fixed; tools were out of scope.

## From the inbound-media run (2026-08-25)

- **Order-ID normalization for real tools.** Whisper renders pt-BR numbers
  with a thousands separator ("12.345"), so the agent may call customer
  tools with `order_id="12.345"`. Only the demo mock
  (`scripts/mock_tool_server.py`) normalizes digits today. Options: a
  per-tool "normalize digits" flag on tool parameters, or a default agent
  instruction to strip separators from identifiers before tool calls.
- **Outbound media.** TTS voice replies, operator/agent sending images
  (needs per-adapter send-media and an upload path on `/operator`).
- **Media retention/cleanup.** `MEDIA_ROOT` grows forever; no TTL, no
  orphan sweep, no size accounting per tenant.
- **Remote storage.** `LocalMediaStorage` is disk-only; S3-compatible
  backend behind the same `put/get/path` interface.
- **Live Evolution media probe.** Ticket 29 is test-proven only; no real
  Evolution instance was available.

## From the boundaries run

- **Inbound screening.** The customer's message is not checked before the
  agent runs, so an off-topic or "ignore your instructions" message still
  reaches the model. A cheap pre-check on the debounced batch, shaped like
  the sentiment classifier, would catch these earlier.
- **One retry before refusing.** A blocked reply is replaced by the refusal
  at once. Asking the agent once more, with the block reason, could recover
  replies that were only slightly out of line.
- **Handoff after repeated blocks.** Several blocked replies in one
  conversation should hand off to a human through `handoff_with_summary`.
- **Tenant-wide boundaries.** Boundaries exist per agent and as the server
  floor only. A tenant cannot define one set shared by all its agents.
- **Operator page screens.** No UI to edit boundaries or browse blocked
  replies. Both are API and database only (`agent_runs.blocked_text`).
- **Measure block and false-block rates on a real model.** The tests use a
  scripted model, so it is unknown how often a real model breaks a boundary
  and how often the check blocks a fine reply.

## From the reliability run

- **Resend only the missing bubbles.** A reply with a failed bubble is
  dead-lettered as partly delivered and the rest is dropped. Sending only the
  undelivered bubbles would finish the reply without repeating the first.
- **API or page for the dead-letter list, and replay.** `harness:dlq:turns`
  is only readable with `redis-cli LRANGE`. Nothing lists it, removes
  entries or puts a batch back for another turn.

- **A failed turn that lost its lock can be answered again.** If a turn
  failed after it delivered bubbles and then found its lock gone, it is not
  dead-lettered; the current holder of the batch can answer it again.
- **A crash while dead-lettering can double the entry.** A crash between
  writing the dead-letter entry and finishing the batch can produce a second
  entry and a second error message.
- **Test seams in the scheduler.** `_pause` and `_exclusive_sessions` in
  `app/workers/scheduler.py` exist only because tests patch `asyncio.sleep`
  and share one database session. The tests should be changed so these can
  go.
- **The production scheduler path is thinly tested.** Most tests drive
  `tick`; `run_scheduler`, which the worker runs, is covered only in
  `tests/test_scheduler_concurrency.py`.

## From the reliability run, part 2

- **Rows written before a slow call stay when the turn later fails.** Each
  `end_transaction` commits what the turn had so far, so a failed turn leaves
  its earlier messages and run row behind instead of rolling them back.
- **The new intermediate commits are not preceded by a lock check.** A turn
  that lost its lock can still commit rows before it notices.
- **A reply row is stored after its send.** A crash between the two repeats
  the reply. A lock loss after the send still rolls back the
  unsupported-media and handoff reply rows.
- **A handoff whose message never goes out.** A handoff is committed before
  its message is sent. Any later run of the same batch sends the message (a
  retry after a failed send, or the batch a crashed worker left in flight,
  once the claim times out). A batch that runs out of attempts (three failed
  sends) still ends without it: the dead-letter path's failure notice is sent
  only to `ACTIVE_AI` conversations, so the customer hears nothing.
- **A reused lookup result can be seconds old.** Reuse does not check age.
- **Unknown-outcome calls are visible only in logs and `tool_executions`.**
  No page or API lists rows left `started`.
- **API request paths still hold a transaction during media fetch.** Only the
  worker's turn was changed.
- **Transport errors are not retried.** The short retry covers 429/5xx only;
  a timeout or connection error is not retried.
- **A retried turn's prompt contains the earlier attempt's saved tool
  messages.**
- **One unexplained failure of `tests/test_e2e_demo.py`.** It failed once in a
  full run during this work and did not reproduce.
- **Tested with fakeredis and `respx` only.** No run against a real Redis,
  model endpoint or channel.

## From the conversation-correctness run

- **A tool call in flight completes after an operator takes over.** The state
  check runs before each send, not during a tool call.
- **A cut-off reply is not explained to the customer.** After an operator
  stops a turn, the customer sees the bubbles already sent and nothing more.
- **A person seen under an `@lid` and a phone number is two contacts.** Not
  merged. Delivery to an `@lid` is unverified against a real Evolution
  instance.
- **Migration `0012` closes duplicates and its downgrade does not reopen
  them.** It drops the index only.
- **A turn stopped by an operator is recorded only as a log line.** The stop
  is the log event `turn.stopped_state_changed`; the turn's `AgentRun` stays
  successful with the full reply text, although the customer got none or only
  part of it.
- **Migration `0012` blocks writes to `conversations` while it runs.** It
  takes `LOCK TABLE conversations IN SHARE ROW EXCLUSIVE MODE` and builds the
  index without `CONCURRENTLY`; on a large table, run it in a quiet period.
- **Still open from PRD epic E4 (done in part 2 below: E4.4, E4.7, E4.8, E4.9, E4.10):** E4.4 (media fetched inline, base64 in
  stored payloads), E4.7 (sentiment prompt contains the batch twice), E4.8
  (rolling-summary cursor, timestamp sources, tool rows in the context
  window), E4.9 (wrong channel type returns 500), E4.10 (validation error
  format).
- **Message order depends on two clocks. (Done: ordering uses `Message.seq`;
  see part 2.)** Inbound messages get `created_at`
  from the database (`now()`); assistant and tool messages from the app
  (`datetime.now(UTC)` in `app/conversations/turn.py`,
  `app/agents/runtime.py`, `app/api/conversations.py`). When the database
  clock is behind the app clock, a customer message stored right after a
  reply sorts before it. Seen with the test database container 25 s to 3 min
  behind the host (Docker VM clock drift): `tests/test_handoff.py`,
  `tests/test_e2e_demo.py`, `tests/test_lock_heartbeat.py` and
  `tests/test_scheduler_concurrency.py` then fail every time and pass again
  once the clocks agree. The same can happen in production with a smaller gap.
- **Tests reach the network. (Done: a guard in `tests/conftest.py` fails any
  test that tries; the handoff brief is faked.)**
  `tests/test_handoff.py::test_handoff_tool_flips_state_sends_message_and_silences_ai`
  (and the e2e demo) request `https://api.openai.com/v1/chat/completions` for
  the handoff brief (answered `401`, logged as `handoff.summary_failed`).
- **Migration `0013` rewrites every message row under an exclusive lock.**
  It numbers all existing `messages` rows in one transaction holding an
  `ACCESS EXCLUSIVE` lock on the table, so reads and writes of `messages`
  (webhooks, turns, the operator view) block until it ends; the time grows
  with the table. Run it in a quiet period. Batching it was left out.
- **A timing-dependent test.** *Done in part 2.*
  `tests/test_scheduler_concurrency.py::test_run_scheduler_starts_newly_due_turn_while_slow_one_runs`
  failed under load. The cause was not its sleeps: it read the reply from
  the database the instant the adapter saw it, and the row is stored after
  the send. It now waits for the row.

## From the conversation-correctness run, part 2

- **Base64 remains in payloads stored before this run.** Only new messages are
  stored without it; cleaning old rows is irreversible and was left out.
- **No cap on the size of the prompt.** `context_message_limit` counts customer,
  operator and assistant messages; tool rows do not count, so a tool-heavy
  agent sends a longer prompt than before.
- **A failed media download is not retried.** The message becomes `unsupported`
  / `[mídia não recebida]` and the customer gets the unsupported-media reply.
- **Inline base64 sent by Evolution is not used.** It is stripped from the
  stored event, and the worker always downloads the file itself.
- **`created_at` still comes from two clocks.** Database `now()` for inbound
  rows, the app's clock for replies and operator messages. It is display only;
  order is `seq`. The `after=` filter of
  `GET /conversations/{id}/messages` still compares `created_at`.
- **Migration `0013` rewrites every message row under an exclusive lock.**
  It numbers all existing `messages` rows in one transaction holding an
  `ACCESS EXCLUSIVE` lock on the table, so reads and writes of `messages`
  (webhooks, turns, the operator view) block until it ends; the time grows
  with the table. Run it in a quiet period. Batching it was left out.

## From the categories run

- **One more model call per batch.** With categories defined, every batch of
  customer messages, and every batch during a handoff, costs one classifier
  call. It is not counted in an agent run's `llm_calls`. There is no setting to
  turn it off per agent; a tenant with no categories pays nothing.
- **No unlock of a human's choice.** Once `PUT /conversations/{id}/category` is
  used, the model never changes that conversation, and no endpoint undoes the
  lock. Setting `null` clears the category and also locks it. Deleting the
  category leaves the conversation uncategorized and still locked.
- **Claims never expire and the releaser is not checked.** An operator who
  leaves holds the conversation until someone calls `release`, `resume` or
  `close`; anyone with the tenant key can release any claim. The `operator_id`
  is stored as given, not checked against anything.
- **No priorities, counts or reports.** No per-category counts, no wait-time
  alerts, no ordering other than `waiting_since`.
- **No audio transcription for categorization during a handoff.** The branch
  that categorizes a `HUMAN_HANDOFF` batch reads the messages as stored; a voice
  note is read only if it was already transcribed.
- **No category admin on the demo page.** `/operator` can move a conversation
  and claim it, but categories are created and edited through the API only.
- **No tab counters on `/operator`**, and its click flows (Filas, Minhas,
  claim, release, the category selector) have no automated browser test; the
  API behind them is tested.
- **A human's choice during a model call is dropped silently.** When the
  conversation is locked while the classifier runs, nothing is written and
  nothing is logged.
- **`GET /lines` caps the total, not each line.** With `limit` reached, the
  newest waiting conversations are cut, whichever line they are in.
- **A conversation that is not handed off has no wait time.** `waiting_since`
  is only set by a handoff. Migration `0015` backfilled every conversation
  already in `HUMAN_HANDOFF` with the `created_at` of its newest unresolved
  handoff, or the migration time when it had none.

## From the API surface run

- **The demo pages do not use `after_seq`.** `/chat` and `/operator` still load
  the latest page of messages on every poll instead of asking for what is new.
- **`GET /lines` and the memories list are not paged.** Both take a capped
  `limit` and no cursor.
- **Stored rows are not checked against the new bounds.** An agent with a
  temperature of 50 or a tool named `remember` that was stored before this run
  stays as it is; the bounds apply only to values sent to the API. No migration
  cleans them.
- **`last_accessed_at` is unused.** The column and the `MemoryOut` field stay,
  but nothing writes them any more.
- **Indexes were built without `CONCURRENTLY`.** Migration `0016` blocks writes
  to `handoffs`, `memories` and `conversations` while it runs; on a large table,
  run it in a quiet period.
- **No total counts.** A paged list gives no row count and no "last page"
  number, only the missing `X-Next-Cursor` header.
- **Lists return at most 100 rows by default.** A caller that relied on getting
  every row must page.
- **A duplicate memory is found by exact text only.** Case and spacing are
  ignored; a reworded fact is stored again. Expired memories do not count as
  duplicates.
- **Pass the same filters with a cursor.** The cursor holds only the sort key
  of the last row, so a changed filter is not checked against it.

## From the CI run

- **No coverage measurement.** Neither `make check` nor CI reports coverage or
  enforces a threshold.
- **Tests are not type-checked.** mypy runs over `app/` only.
- **The rest of `app/` is not strict.** Only `app.security`, `app.queue` and
  `app.conversations` are.
- **Two `type: ignore` remain in `app/`**, each with its reason on the line:
  `app/conversations/turn.py` (`[call-arg]`: `message_ids` is optional and not
  in the runtime Protocol) and `app/workers/scheduler.py` (`[misc]`: mypy cannot
  infer a lambda with a default-bound argument). Many other fixes are
  `cast(Any, ...)` on Redis pipeline calls and SQL `tuple_` arguments.
- **CI does not build the Docker image.** A broken `Dockerfile` or compose file
  is not caught.
- **No real Redis in tests.** The suite uses `fakeredis`, and CI has no Redis
  service.
- **Branch protection is not set.** A failing workflow does not stop a merge.
- **The workflow has never run on GitHub.** It is only checked by
  `tests/test_ci_workflow.py`, which reads the file.
- **`/ready` does not check the worker or the model endpoint.** It checks the
  database and Redis only, and not the media disk.
- **`make db` ignores a `POSTGRES_PORT` set only in `.env`.** Make does not read
  `.env`, so the probe uses the shell environment or 5434.

## From the ops run

PRD epics E1 to E7 are now done. What is left over:

- **E7.6 was left out.** Per-tenant LLM keys and base URL, token and cost
  accounting, and metrics. A candidate for its own grill.
- **Names still say `harness`.** The package, the API title and the docs say
  parley. The Redis keys (`harness:jobs`, `harness:dlq:turns`, ...), the
  database name and user (`harness`, and `harness_test` for the tests), the
  compose defaults and the `harness_operator_api_key` browser storage key were
  not renamed: renaming them needs a data migration.
- **Transcription language is per deployment.** `STT_LANGUAGE` (default `pt`)
  applies to every agent and tenant. Transcription runs before an agent is
  known.
- **The default texts are Portuguese only.** The technical-error, unsupported
  media and boundary-refusal defaults, and the two placeholders, are not
  translated, and there is no language setting that switches them. An agent can
  set `error_message` and `unsupported_media_message`; the placeholders are
  deployment-wide.
- **An existing media volume needs one manual `chown`.** The image runs as `app`
  (uid 10001). A `media_data` volume made by an older image stays owned by root
  until the operator changes it (see README, "The stack").
- **The image is not built in CI.** A broken `Dockerfile` or compose file is
  still not caught, and the non-root user and the `migrate` start order are
  checked only by tests that read the files.
- **Default database credentials remain.** Compose falls back to
  `harness`/`harness` when `POSTGRES_USER` and `POSTGRES_PASSWORD` are not set.
  The database and Redis are no longer published unless the dev file is used.
- **Leftover wording.** Comments in `app/agents/sentiment.py`,
  `app/agents/runtime.py`, `app/repositories/messages.py`,
  `app/models/agent.py`, `app/conversations/lock.py` and
  `app/media/storage.py` still say "the ticket" or "later tickets". A few
  others still name a ticket by number: `app/agents/context.py` ("Ticket 28"),
  `app/static/chat.html`, and a comment in `docker-compose.yml` ("ticket 26").
- **No test starts the stack.** Nothing in the suite runs `--scale api=2`; the
  compose tests only read the files.

## Older known MVP limitations (README)

- **SSRF DNS-rebinding TOCTOU.** The allowed-IP check resolves once;
  `httpx` re-resolves on connect. It now also applies to channel endpoints
  (Evolution `base_url`). Fix: pin the validated IP via a custom
  transport/resolver.
- **`max_iterations` as a real `AgentRunStatus` member.** Still recorded
  as `ERROR` + `error="max_iterations"`. Migration pattern now exists
  (`0007_agent_run_superseded.py`), so this is a small ticket.
- **Semantic long-term memory.** `MemoryService.search()` is `ILIKE`;
  interface ready for vector similarity (pgvector).
- **Background memory extraction.** `remember` only fires when the model
  calls it; nothing summarizes a conversation into memories afterwards.
- **REST-poll only.** No WebSocket/SSE for `/chat` and `/operator`.

## Product-surface gaps left out of scope by recent runs

- Sentiment: per-tenant/channel threshold, cooldown, score persistence.
- Humanizer: per-channel pacing settings (global only today).
- Barge-in: "user is typing" as pre-emptive pause; retracting sent
  bubbles.
- Operator UI: accounts/roles/assignment, notifications, unread counters,
  run/tool/sentiment supervision view.
- Production hardening: real auth (static API keys today), metrics/
  tracing, deploy (see `deploying-plugu-projects` skill for Dokploy).

## From the business tools run

- **No signature on tool calls.** A call to an MCP server carries the customer
  context in `_meta` (and an HTTP tool in `X-Parley-*` headers), protected only
  by the token, TLS and the address rules; whoever holds the token can claim any
  `parley/user_id`. Fix: a timestamp and an HMAC with a secret shown once, in the
  style of the signed event pushes (`app/events/signing.py`), with a check
  sample in `docs/guides/business-tools.md`.
- **Single tools per agent.** An agent lists whole servers (`mcp_servers`);
  `allowed_tools` on the server narrows what is imported for every agent that
  lists it. Picking tools per agent needs a field of its own.
- **Renaming a server detaches it.** An agent lists server names, so `PATCH
  /mcp-servers/{server_id}` with a new `name` leaves the agents that list the old
  one with no tools of that server, with no warning.

## From the model per job run

- **Typed answers on a chat model.** A job set to `llm:<model>` asks in text and parses
  the text, as before. Only `typesafe:` jobs get typed answers. They come to chat
  models with native providers (`docs/roadmap/3-native-providers.md`).
  **Done** for `llm:`, `openai:`, `anthropic:` and `google:` jobs (the library's tool
  mode, one retry, then `check_unparseable`). A job left unset still runs on the
  agent's model in text; typed answers for it are open (see the next section).

## From the native providers run

- **Retire the hand-written provider.** `openai_compatible` is still
  `app/llm/providers/openai_compatible.py`, with its own retry in `app/http_retry.py`.
  Running it through Pydantic AI as well would leave one path; it was not done, so
  that no existing deployment changes.
- **Credentials per tenant.** `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and
  `GOOGLE_API_KEY` are one per server. A tenant cannot bring its own.
- **Prompt caching.** Not used on any provider. Add it only with measured cost and
  latency from a real run.
- **Typed answers for unset jobs.** A job with an empty setting keeps the agent's
  model and the text prompt, whatever the provider.
- **No real vendor was called.** Image formats, token reporting, tool call ids
  across a switch and OpenAI's strict mode on the pick-one schemas are unverified
  (`scripts/demo_native_providers.py`, see operations).
- **Settings per agent or per tenant.** `BOUNDARY_CHECK_MODEL`, `SENTIMENT_MODEL`,
  `CATEGORY_MODEL`, `SUMMARY_MODEL` and `HANDOFF_BRIEF_MODEL` are environment
  settings, the same for every tenant and agent. A tenant that must not have its
  text sent to TypeSafe cannot opt out alone.
- **A pre-check job.** Judging the customer's message before the agent answers (a
  cheap decision to skip an expensive reply) is not built.
- **Comparing sentiment and department.** `python -m app.llm.compare` replays the
  boundary check only; the other two decisions have no stored result to compare
  against and no command.
- **The recorded boundary reason on a decision model is a hint.** `allowed` decides
  and `broken` names the rule; they are separate answers and can disagree.

## From the session memory run

- **Facts that expire.** A lasting fact stays until it is deleted or has an
  `expires_at`; the session-end call can add or rewrite a fact but never sets an
  expiry or drops one, so "lives in Recife" outlives a move unless a later session
  contradicts it.
- **Searching old messages.** A new session sees no message of an earlier one,
  only its recap. A `search_history` built-in over the contact's old messages is
  not built.
- **Per-customer settings.** `session_gap_hours`, `session_recap_limit` and
  `session_recap_max_age_days` are per agent; a customer cannot have their own, and
  there is no way to ask the agent to forget one customer's recaps except deleting
  them through the memories API.
- **Vendezap closing on a finished pedido.** Vendezap does not close the parley
  conversation when a pedido is done, so the recap waits for `session_gap_hours` of
  silence. Calling `POST /conversations/{conversation_id}/close` there would write it
  at once (see `docs/integrations/vendezap.md`).
- **A failed recap is not retried by itself.** After 3 tries a session is `failed`
  with no recap; resetting it is a manual `UPDATE` (see operations).
