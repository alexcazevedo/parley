# Follow-ups

Known gaps and next candidates, collected across forge runs. Each is a
plausible `/forge:idea` starting point. Not ordered by priority.

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
  tracing, CI, deploy (see `deploying-plugu-projects` skill for Dokploy).
