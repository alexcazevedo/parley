# Graph Report - conversational-harness  (2026-08-25)

## Corpus Check
- 152 files · ~52,989 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1234 nodes · 2639 edges · 108 communities (81 shown, 27 thin omitted)
- Extraction: 64% EXTRACTED · 36% INFERRED · 0% AMBIGUOUS · INFERRED: 953 edges (avg confidence: 0.63)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `fd19d194`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 40|Community 40]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]
- [[_COMMUNITY_Community 60|Community 60]]
- [[_COMMUNITY_Community 61|Community 61]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 63|Community 63]]
- [[_COMMUNITY_Community 64|Community 64]]
- [[_COMMUNITY_Community 65|Community 65]]
- [[_COMMUNITY_Community 66|Community 66]]
- [[_COMMUNITY_Community 67|Community 67]]
- [[_COMMUNITY_Community 68|Community 68]]
- [[_COMMUNITY_Community 69|Community 69]]
- [[_COMMUNITY_Community 70|Community 70]]
- [[_COMMUNITY_Community 71|Community 71]]
- [[_COMMUNITY_Community 72|Community 72]]
- [[_COMMUNITY_Community 74|Community 74]]
- [[_COMMUNITY_Community 75|Community 75]]
- [[_COMMUNITY_Community 76|Community 76]]
- [[_COMMUNITY_Community 77|Community 77]]
- [[_COMMUNITY_Community 78|Community 78]]
- [[_COMMUNITY_Community 79|Community 79]]
- [[_COMMUNITY_Community 80|Community 80]]
- [[_COMMUNITY_Community 81|Community 81]]
- [[_COMMUNITY_Community 82|Community 82]]
- [[_COMMUNITY_Community 83|Community 83]]
- [[_COMMUNITY_Community 84|Community 84]]
- [[_COMMUNITY_Community 85|Community 85]]
- [[_COMMUNITY_Community 86|Community 86]]
- [[_COMMUNITY_Community 87|Community 87]]
- [[_COMMUNITY_Community 88|Community 88]]
- [[_COMMUNITY_Community 89|Community 89]]
- [[_COMMUNITY_Community 90|Community 90]]
- [[_COMMUNITY_Community 91|Community 91]]
- [[_COMMUNITY_Community 92|Community 92]]
- [[_COMMUNITY_Community 93|Community 93]]
- [[_COMMUNITY_Community 94|Community 94]]
- [[_COMMUNITY_Community 95|Community 95]]
- [[_COMMUNITY_Community 96|Community 96]]
- [[_COMMUNITY_Community 105|Community 105]]
- [[_COMMUNITY_Community 106|Community 106]]

## God Nodes (most connected - your core abstractions)
1. `AgentRuntime` - 64 edges
2. `FakeLLMProvider` - 52 edges
3. `LLMResponse` - 50 edges
4. `RedisQueue` - 41 edges
5. `Message` - 39 edges
6. `ToolRegistry` - 37 edges
7. `Channel` - 33 edges
8. `Tenant` - 31 edges
9. `Conversation` - 31 edges
10. `MessageRole` - 31 edges

## Surprising Connections (you probably didn't know these)
- `test_unknown_tool_is_reported_back_to_the_model()` --calls--> `Next`  [INFERRED]
  tests/test_agent_loop.py → STATE.md
- `test_humanized_turn_marks_read_then_paces_each_part()` --calls--> `get_settings()`  [INFERRED]
  tests/test_turn_humanize.py → app/config.py
- `RaisingProvider` --uses--> `ToolRegistry`  [INFERRED]
  tests/test_handoff_summary.py → app/tools/registry.py
- `MustNotBeCalledProvider` --uses--> `ToolRegistry`  [INFERRED]
  tests/test_handoff.py → app/tools/registry.py
- `StubExecutor` --uses--> `ToolRegistry`  [INFERRED]
  tests/test_agent_loop.py → app/tools/registry.py

## Communities (108 total, 27 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (54): maybe_summarize(), Rolling conversation summary: keeps the LLM's context bounded without losing old, Advance the rolling summary if enough new messages have piled up.      Never rai, _render(), _summarize(), _abandon_turn(), _build_reply(), _deliver() (+46 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (51): claim(), drain_pending(), due_conversations(), has_pending(), pending_key(), push_pending(), Per-conversation debounce state in Redis.  Two keys per conversation:  - ``harne, Queue a message for the conversation and (re)arm its debounce window. (+43 more)

### Community 2 - "Community 2"
Cohesion: 0.09
Nodes (39): AgentCreate, AgentOut, AgentUpdate, ChannelCreate, ChannelOut, ChannelUpdate, bootstrap_chat(), BootstrapOut (+31 more)

### Community 3 - "Community 3"
Cohesion: 0.12
Nodes (34): ConversationOut, HandoffCreate, HandoffOut, MessageCreate, MessageOut, The warm-handoff operator brief — the newest `Handoff` row for a     conversatio, _Delivery, EchoRuntime (+26 more)

### Community 4 - "Community 4"
Cohesion: 0.09
Nodes (26): ChannelAdapter, ChannelAdapter, IncomingMessage, OutgoingMessage, Channel adapter interface.  Every inbound transport (Web Chat REST, WhatsApp/Evo, One implementation per `app.models.channel.ChannelType`., Mark an inbound message as read on the channel's transport.          No-op by de, Show a "typing…"/composing indicator on the channel's transport.          No-op (+18 more)

### Community 5 - "Community 5"
Cohesion: 0.08
Nodes (32): Base, What a turn needs from an agent implementation: text in, text out., A runtime that persists rows of its own and so needs the turn's session., SessionAwareTurnRuntime, TurnRuntime, DeclarativeBase, Agent, Agent model: an LLM persona bound to instructions, model config, and tools. (+24 more)

### Community 6 - "Community 6"
Cohesion: 0.07
Nodes (35): Application settings, loaded from environment / .env., Settings, BaseSettings, _chunk_sentences(), _overlaps_any(), Reply splitting + typing-pace calculation for the humanizer (ticket 14).  Pure,, Split reply text into chat-bubble-sized parts.      1. Primary: split on blank l, Simulated typing delay for one bubble, clamped to configured bounds. (+27 more)

### Community 7 - "Community 7"
Cohesion: 0.07
Nodes (23): create_contact(), create_contact_memory(), delete_contact(), delete_contact_memory(), from_model(), get_contact(), _get_contact_or_404(), list_contact_memories() (+15 more)

### Community 8 - "Community 8"
Cohesion: 0.08
Nodes (29): The provider this runtime would use for `agent`.          Exposed so `app.conver, _build_prompt(), _parse_score(), Frustration classifier: one best-effort LLM call scoring user frustration.  Runs, Best-effort frustration score in [0.0, 1.0], or `None` on any failure.      Neve, _recent_messages(), _score(), score_frustration() (+21 more)

### Community 9 - "Community 9"
Cohesion: 0.1
Nodes (20): _drain_jobs(), fake_dns(), _messages(), End-to-end proof of the PRODUCT.md "success demo", over the real pipeline (inges, `mock-tools.test` resolves to a public address; everything else fails.      resp, Tenant + get_order tool + agent (memory/handoff on, get_order wired) +     webch, _registry(), _run_turn() (+12 more)

### Community 10 - "Community 10"
Cohesion: 0.1
Nodes (18): LLMError, LLMProvider, LLMUsage, Provider-agnostic LLM types.  Everything above this module (context builder, age, A tool as advertised to the model. `parameters` is a JSON Schema object., Any failure talking to the provider (transport, HTTP status, bad body)., The single seam between the agent runtime and any model backend., ToolSpec (+10 more)

### Community 11 - "Community 11"
Cohesion: 0.11
Nodes (23): get_adapter(), Maps `Channel.type` to its `ChannelAdapter` implementation., The SSRF guard: what a tenant-controlled tool endpoint is allowed to reach.  DNS, test_allowlist_overrides_the_ip_checks(), test_allowlist_rejects_hosts_outside_it(), test_disabled_flag_still_rejects_non_http_schemes(), test_guard_can_be_disabled_for_development(), test_host_resolving_to_both_public_and_private_is_rejected() (+15 more)

### Community 12 - "Community 12"
Cohesion: 0.25
Nodes (23): _drain_jobs(), _executions(), fake_dns(), _provider(), Customer HTTP tools end-to-end: the real pipeline (ingest -> debounce -> schedul, Scoped to this conversation: the API layer commits, so rows written by     earli, `example.test` resolves to a public address; everything else fails.      respx i, Tenant + agent (wired to `get_order`) + tool row + webchat channel. (+15 more)

### Community 13 - "Community 13"
Cohesion: 0.21
Nodes (17): ContextBuilder, Builds the prompt transcript for one agent turn.  Short-term memory is the tail, Rehydrate the tool calls the runtime stashed in `raw_payload`., System prompt + the last `agent.context_message_limit` messages.          `tools, _tool_calls_from(), _agent(), _conversation(), _memory() (+9 more)

### Community 14 - "Community 14"
Cohesion: 0.14
Nodes (15): close_conversation(), get_conversation(), _get_conversation_or_404(), handoff_conversation(), _latest_handoff(), list_conversation_messages(), list_conversations(), post_operator_message() (+7 more)

### Community 15 - "Community 15"
Cohesion: 0.17
Nodes (16): _drain_jobs(), _handoffs(), Sentiment-triggered escalation wired into `process_turn` (ticket 21).  Same pipe, One full turn; returns (conversation_id, runtime, adapter, scorer)., Stands in for the real adapter: records outbound calls, sends nothing., RecordingAdapter, _replies(), _run() (+8 more)

### Community 16 - "Community 16"
Cohesion: 0.13
Nodes (11): AgentRuntime, The agent loop: context -> LLM -> tools -> reply.  One `run_turn` call is one `A, The humanizer's prompt hint, when this conversation's channel wants it., Long-term facts folded into the system prompt (see `ContextBuilder`).          G, Record the assistant's tool-call step so later turns can replay it., A `SessionAwareTurnRuntime` (see app/conversations/turn.py).      `provider` is, LLMMessage, One entry of the prompt transcript sent to the model. (+3 more)

### Community 17 - "Community 17"
Cohesion: 0.17
Nodes (15): _drain_jobs(), The humanizer wired into `process_turn` (ticket 16).  Same pipeline as `tests/te, One full turn; returns (conversation_id, adapter, events, delays)., Guards the monkeypatch above: the pacing pause must stay `asyncio.sleep`., Stands in for the real adapter: records the outbound call sequence.      `send_m, RecordingAdapter, _replies(), _run() (+7 more)

### Community 18 - "Community 18"
Cohesion: 0.14
Nodes (11): Queue, Job, Redis-backed `Queue`: a simple `LPUSH`/`BRPOP` list, with a dead-letter list for, RedisQueue, test_job_lands_in_dlq_after_max_attempts(), test_run_once_returns_none_on_empty_queue(), test_worker_run_once_dequeues_logs_and_acks(), main() (+3 more)

### Community 19 - "Community 19"
Cohesion: 0.17
Nodes (14): post_dev_message(), Dev simulator: `POST /dev/messages` is a thin wrapper over the exact same ingest, ingest_message(), post_webchat_message(), Web Chat REST ingestion endpoint. The client polls `GET /conversations/{id}/mess, resolve_channel(), Channel-specific inbound webhooks.  Unlike Web Chat / dev (`app/api/webchat.py`,, whatsapp_webhook() (+6 more)

### Community 20 - "Community 20"
Cohesion: 0.19
Nodes (17): _admin_key(), _auth(), _create_channel(), _create_tenant(), Rate limiting, message size limits, safe error responses., 500 error should not leak internals., Webhook with oversized message should truncate and return 200., 31st message from same user in a minute → 429. (+9 more)

### Community 21 - "Community 21"
Cohesion: 0.12
Nodes (6): BuiltinExecutor, Harness-provided tools, individually gated per agent by a boolean flag     on `A, default_executors(), Merges tool executors into the single view the agent runtime consumes.  Ordering, What a production run gets, in priority order.      `HttpToolExecutor.can_handle, Every tool `agent` may call, first-registration-wins on duplicates.

### Community 22 - "Community 22"
Cohesion: 0.13
Nodes (8): failure(), NoopExecutor, Tool execution contracts.  A `ToolExecutor` is one *source* of tools, not one to, One source of callable tools., True if `execute` should be routed this tool name., Tools this executor offers to `agent` (already tenant-scoped)., Terminal fallback: advertises nothing, fails every call.      Keeps the runtime, ToolExecutor

### Community 23 - "Community 23"
Cohesion: 0.3
Nodes (13): _drain_jobs(), _message_count(), Rolling conversation summary: triggers at `agent.summary_threshold`, never touch, _run_turn(), _send(), _session_factory(), _setup(), test_maybe_summarize_carries_prior_summary_into_the_prompt_and_advances_cursor() (+5 more)

### Community 24 - "Community 24"
Cohesion: 0.36
Nodes (14): A single tool invocation requested by the model., ToolCall, _agent_run(), _drain_jobs(), _messages(), The agent loop end-to-end over the real pipeline: ingest -> debounce -> schedule, _run_turn(), _send() (+6 more)

### Community 25 - "Community 25"
Cohesion: 0.42
Nodes (14): _admin_key(), _auth(), _create_evolution_channel(), _create_tenant(), _fixture(), Evolution API (WhatsApp) adapter: authenticated webhook ingestion and outbound `, test_duplicate_key_id_inserts_once(), test_from_me_is_ignored_with_200_and_no_row() (+6 more)

### Community 26 - "Community 26"
Cohesion: 0.44
Nodes (13): _contact_id(), _drain_jobs(), Long-term memory: the `remember` built-in tool end-to-end, REST CRUD, tenant iso, _run_turn(), _send(), _session_factory(), _setup(), test_cross_tenant_cannot_read_or_delete_memory() (+5 more)

### Community 27 - "Community 27"
Cohesion: 0.35
Nodes (13): _drain_jobs(), _handoffs(), RaisingProvider, The warm-handoff operator brief: `app.handoff.summarizer.handoff_with_summary` p, Always fails — proves the summarizer's LLM failure path is harmless., _run_turn(), _send(), _session_factory() (+5 more)

### Community 28 - "Community 28"
Cohesion: 0.42
Nodes (13): _drain_jobs(), _messages(), Human handoff: the `handoff_to_human` built-in tool end-to-end, the handoff/resu, _run_turn(), _send(), _session_factory(), _setup(), test_handoff_disabled_agent_does_not_advertise_or_execute() (+5 more)

### Community 29 - "Community 29"
Cohesion: 0.29
Nodes (11): LLMResponse, FakeLLMProvider, Scripted provider for tests (and offline demos).  Lives in `app/` rather than `t, Returns `scripted` responses in order, recording every call.      With `repeat_l, `GET /conversations/{id}` exposes `latest_handoff` (reason, summary, requested_b, _send(), _setup(), test_conversation_without_handoff_has_null_latest_handoff() (+3 more)

### Community 30 - "Community 30"
Cohesion: 0.14
Nodes (13): Agent loop, Architecture, Barge-in, code:block1 (app/), code:block2 (tenants (admin-created; api_key_hash)), Data model, Extension points, Humanizer (+5 more)

### Community 31 - "Community 31"
Cohesion: 0.32
Nodes (10): _admin_key(), _auth(), _create_tenant(), CRUD REST coverage for agents, channels, tools, contacts, plus read-only convers, test_agent_crud_lifecycle(), test_channel_crud_and_secret_never_returned(), test_contact_crud_and_unique_per_channel(), test_conversations_are_read_only_and_empty_by_default() (+2 more)

### Community 32 - "Community 32"
Cohesion: 0.17
Nodes (11): API overview, Architecture (at a glance), code:block1 (┌──────────────┐        ┌───────────────┐), code:bash (cp .env.example .env), code:bash (uv run ruff check .), Conversational Agent Harness, Evolution API (WhatsApp) setup, Known MVP limitations (+3 more)

### Community 33 - "Community 33"
Cohesion: 0.31
Nodes (10): _get_messages(), _load_env_file(), main(), _print_transcript(), Scripted end-to-end demo against a *running* `docker compose up` stack — the PRO, Poll until the transcript grew past `baseline_count` AND the newest     message, `.env` values, with real environment variables taking priority —     mirrors pyd, _settings() (+2 more)

### Community 34 - "Community 34"
Cohesion: 0.2
Nodes (9): add_exception_handlers(), generic_exception_handler(), http_exception_handler(), Global exception handler: safe error responses with request_id., HTTPException handler: wrap in our error format., Validation errors (422) keep field detail., Generic exception handler: never leak internals., Register exception handlers on the FastAPI app. (+1 more)

### Community 35 - "Community 35"
Cohesion: 0.47
Nodes (9): _admin_key(), _auth(), _create_channel(), _create_tenant(), Ingestion pipeline: Web Chat + dev simulator -> idempotent persist -> contact/co, test_dev_messages_uses_same_pipeline(), test_duplicate_message_id_inserts_once_and_enqueues_once(), test_two_users_get_two_conversations_same_user_reuses_one() (+1 more)

### Community 36 - "Community 36"
Cohesion: 0.42
Nodes (8): create_tool(), delete_tool(), _encode_auth_config(), get_tool(), list_tools(), CRUD REST for tools. `auth_config` is write-only: encrypted on the way in via `a, _repo(), update_tool()

### Community 37 - "Community 37"
Cohesion: 0.31
Nodes (7): _get_or_create_contact(), _get_or_create_conversation(), Find-or-create contact + conversation for an inbound message.  A contact is uniq, resolve_contact_and_conversation(), ingest(), IngestResult, Inbound ingestion: resolve contact/conversation, idempotently persist the messag

### Community 38 - "Community 38"
Cohesion: 0.25
Nodes (5): _ensure_test_database_exists(), _ensure_valid_secrets_key(), Shared pytest fixtures: an isolated test DB, fake Redis, and an ASGI client with, `.env`'s SECRETS_KEY is a human-readable placeholder, not a real Fernet     key., test_engine()

### Community 39 - "Community 39"
Cohesion: 0.43
Nodes (7): create_agent(), delete_agent(), get_agent(), list_agents(), CRUD REST for agents. Every route is scoped to the calling tenant., _repo(), update_agent()

### Community 40 - "Community 40"
Cohesion: 0.43
Nodes (7): create_channel(), delete_channel(), get_channel(), list_channels(), CRUD REST for channels. `secret` is write-only: encrypted on the way in via `app, _repo(), update_channel()

### Community 42 - "Community 42"
Cohesion: 0.5
Nodes (7): _admin_key(), _auth(), _create_tenant(), Cross-tenant isolation: tenant B must never see tenant A's rows, and a tool's `a, test_tenant_b_cannot_delete_or_update_tenant_a_resources(), test_tenant_b_cannot_get_tenant_a_tool_and_auth_config_never_returned(), test_tenant_b_cannot_list_or_get_tenant_a_agent()

### Community 43 - "Community 43"
Cohesion: 0.38
Nodes (6): acquire(), lock_key(), Per-conversation advisory lock (`harness:lock:{conv_id}`).  `SET key <token> NX, Return the lock token on success, `None` if someone else holds it., Release only if we still own the lock. True if it was ours., release()

### Community 44 - "Community 44"
Cohesion: 0.33
Nodes (5): rate_limit_tenant(), rate_limit_user(), Rate limiting: per-tenant and per-user (channel_id:user_id).  Uses Redis INCR +, Rate limit by tenant_id. Use as dependency., Rate limit by channel_id:user_id. Use as dependency.

### Community 45 - "Community 45"
Cohesion: 0.4
Nodes (5): generate_api_key(), get_current_tenant(), hash_api_key(), Auth dependencies: admin key for /tenants, per-tenant API key for everything els, require_admin()

### Community 46 - "Community 46"
Cohesion: 0.33
Nodes (5): Test the bootstrap endpoint for the demo webchat., Bootstrap creates a new channel with humanize: true., Bootstrap upgrades existing channel's config if humanize is missing., test_bootstrap_creates_channel_with_humanize(), test_bootstrap_idempotent_upgrade_humanize()

### Community 47 - "Community 47"
Cohesion: 0.33
Nodes (5): Barge-in — interruption handling, Out of scope, Primary user story, Success demo, What / why

### Community 48 - "Community 48"
Cohesion: 0.33
Nodes (5): Blockers, Decisions, Next, Now, STATE — rewrite after every ticket completes; never append. ≤30 lines total.

### Community 49 - "Community 49"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 50 - "Community 50"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 51 - "Community 51"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 52 - "Community 52"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 53 - "Community 53"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 54 - "Community 54"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 55 - "Community 55"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 56 - "Community 56"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 57 - "Community 57"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 58 - "Community 58"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 59 - "Community 59"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 60 - "Community 60"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 61 - "Community 61"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 62 - "Community 62"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 63 - "Community 63"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 64 - "Community 64"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 65 - "Community 65"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 66 - "Community 66"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 67 - "Community 67"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 68 - "Community 68"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 69 - "Community 69"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 70 - "Community 70"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 71 - "Community 71"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

## Knowledge Gaps
- **387 isolated node(s):** `handoff_summary: nullable operator-brief column on handoffs  `handoffs.summary``, `handoff_message: per-agent copy sent when a conversation flips to human handoff`, `summary_cursor: rolling-summary bookmark on conversations  `summary_cursor` is n`, `sentiment_flag: per-agent opt-in for the sentiment escalation classifier  `agent`, `agent_run_superseded: 'superseded' status for barge-in  A turn that is interrupt` (+382 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **27 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `AgentRuntime` connect `Community 16` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 8`, `Community 9`, `Community 10`, `Community 12`, `Community 13`, `Community 17`, `Community 18`, `Community 23`, `Community 24`, `Community 26`, `Community 27`, `Community 28`?**
  _High betweenness centrality (0.095) - this node is a cross-community bridge._
- **Why does `RedisQueue` connect `Community 18` to `Community 1`, `Community 2`, `Community 3`, `Community 5`, `Community 41`, `Community 10`, `Community 9`, `Community 12`, `Community 15`, `Community 17`, `Community 19`, `Community 23`, `Community 24`, `Community 26`, `Community 27`, `Community 28`?**
  _High betweenness centrality (0.084) - this node is a cross-community bridge._
- **Why does `HttpToolExecutor` connect `Community 9` to `Community 5`, `Community 10`, `Community 11`, `Community 12`, `Community 16`, `Community 21`, `Community 23`?**
  _High betweenness centrality (0.053) - this node is a cross-community bridge._
- **Are the 53 inferred relationships involving `AgentRuntime` (e.g. with `ContextBuilder` and `Settings`) actually correct?**
  _`AgentRuntime` has 53 INFERRED edges - model-reasoned connections that need verification._
- **Are the 48 inferred relationships involving `FakeLLMProvider` (e.g. with `LLMMessage` and `LLMResponse`) actually correct?**
  _`FakeLLMProvider` has 48 INFERRED edges - model-reasoned connections that need verification._
- **Are the 49 inferred relationships involving `LLMResponse` (e.g. with `FakeLLMProvider` and `OpenAICompatibleProvider`) actually correct?**
  _`LLMResponse` has 49 INFERRED edges - model-reasoned connections that need verification._
- **Are the 34 inferred relationships involving `RedisQueue` (e.g. with `Job` and `Queue`) actually correct?**
  _`RedisQueue` has 34 INFERRED edges - model-reasoned connections that need verification._