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

- **E3.6: database transaction held across model calls and sleeps.** A turn
  keeps one session (and one pooled connection) open while the model runs and
  while humanized bubbles wait. That is why `WORKER_CONCURRENCY` has to stay
  under the pool size. Doing the slow work outside the transaction would free
  the connection.
- **E3.7: repeated or orphaned tool side effects.** A retried turn runs the
  model and its tools again, so a tool with a side effect can run twice, and
  a failed attempt can leave the effects of its tools behind. Needs
  idempotency keys or a record of tool calls per batch.
- **E3.8: shared HTTP clients, retry on 429/5xx, token-limit parameter
  name.** Not done in the reliability run: HTTP clients are not shared, 429
  and 5xx answers are not retried, and the token-limit parameter name is
  part of the same item.
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
