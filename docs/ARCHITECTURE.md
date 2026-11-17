# Architecture

This document is the map of the codebase for anyone extending the harness.
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
                 └─ handoffs (reason, requested_by, summary, resolved_at)
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

**Warm-handoff brief**: both handoff paths (`handoff_to_human` tool and the
operator-initiated `POST /conversations/{id}/handoff`) go through
`app.handoff.summarizer.handoff_with_summary` instead of calling
`HandoffService.handoff` directly. It performs the state transition, then
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
     `process_turn(session, conversation_id, messages, runtime,
     supersede_check=check)`, where `check` probes
     `debounce.has_pending(redis, conversation_id)` — has a newer message
     landed on the pending list since the drain? If the turn comes back
     `Superseded`, the just-drained batch is put back on the pending list
     via `debounce.requeue_pending` (ahead of whatever interrupted it) instead
     of being counted as processed — see "Barge-in" below;
   - releases the lock; if more messages arrived on the pending list while
     the lock was held (including a requeued superseded batch), re-schedules
     the conversation immediately for another turn.
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
  `debounce.requeue_pending` to put the drained batch back at the head of the
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

## Inbound media

Voice notes and photos go through one channel-independent pipeline in core
— no channel or downstream stage (agent loop, humanizer, sentiment, summary,
barge-in) gets special-cased media behavior (PRODUCT.md).

1. **Adapter -> `IncomingMedia`**: each `ChannelAdapter.parse_incoming` maps
   its own payload into an `IncomingMedia` (bytes + mime + filename) on the
   `IncomingMessage`, exactly like it already maps text — the webchat
   adapter takes it from `POST /channels/webchat/media`'s multipart upload
   (`app/api/webchat.py`), the Evolution adapter fetches the bytes via
   `POST {base_url}/chat/getBase64FromMediaMessage/{instance}` when a
   webhook's `audioMessage`/`imageMessage` arrives.
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
   `"[imagem enviada pelo cliente]"` so the turn still answers.
5. **Degradation**: STT failure (or empty transcript) keeps the message as
   `audio` with a fixed placeholder content (`"[áudio não compreendido]"` /
   `"[áudio sem fala detectada]"`) instead of costing the customer a reply.
   Unsupported types get a fixed polite reply
   (`app.conversations.turn.UNSUPPORTED_MEDIA_REPLY`) when a batch is
   unsupported-only; mixed batches (e.g. a photo + an unsupported sticker)
   run normally, with the unsupported item recorded as its own message.
6. **Rendering**: `/chat` and `/operator` render `type=="image"` as an
   `<img>` thumbnail (click to open full-size) plus any caption, and
   `type=="audio"` as an `<audio>` player plus the transcript text below.
   Both fetch `GET /media/{message_id}` (tenant-scoped, `X-API-Key`
   required) client-side and turn the response into an object URL, since
   `<img>`/`<audio>` tags can't set request headers.

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
`MediaTooLarge`; the webhook logs `webhook.media_fetch_failed`, stores an
`unsupported` message with content `[mídia não recebida]`, writes no file and
still answers 200.

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
