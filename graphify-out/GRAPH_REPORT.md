# Graph Report - conversational-harness  (2026-08-25)

## Corpus Check
- 157 files · ~58,028 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1277 nodes · 2719 edges · 110 communities (82 shown, 28 thin omitted)
- Extraction: 64% EXTRACTED · 36% INFERRED · 0% AMBIGUOUS · INFERRED: 967 edges (avg confidence: 0.63)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `cd29d417`
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
- [[_COMMUNITY_Community 97|Community 97]]
- [[_COMMUNITY_Community 98|Community 98]]
- [[_COMMUNITY_Community 107|Community 107]]
- [[_COMMUNITY_Community 108|Community 108]]

## God Nodes (most connected - your core abstractions)
1. `AgentRuntime` - 64 edges
2. `FakeLLMProvider` - 54 edges
3. `LLMResponse` - 52 edges
4. `RedisQueue` - 42 edges
5. `Message` - 39 edges
6. `ToolRegistry` - 37 edges
7. `Conversation` - 36 edges
8. `Channel` - 33 edges
9. `Tenant` - 31 edges
10. `MessageRole` - 31 edges

## Surprising Connections (you probably didn't know these)
- `test_unknown_tool_is_reported_back_to_the_model()` --calls--> `Next`  [INFERRED]
  tests/test_agent_loop.py → STATE.md
- `test_contact_label_present_on_list_and_single_get()` --calls--> `Next`  [INFERRED]
  tests/test_operator_api.py → STATE.md
- `test_humanized_turn_marks_read_then_paces_each_part()` --calls--> `get_settings()`  [INFERRED]
  tests/test_turn_humanize.py → app/config.py
- `RaisingProvider` --uses--> `ToolRegistry`  [INFERRED]
  tests/test_handoff_summary.py → app/tools/registry.py
- `MustNotBeCalledProvider` --uses--> `ToolRegistry`  [INFERRED]
  tests/test_handoff.py → app/tools/registry.py

## Communities (110 total, 28 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (54): _abandon_turn(), _build_reply(), _deliver(), _deliver_humanized(), _is_superseded(), _mark_read(), _mark_run_superseded(), _maybe_escalate_on_sentiment() (+46 more)

### Community 1 - "Community 1"
Cohesion: 0.07
Nodes (49): claim(), drain_pending(), has_pending(), pending_key(), push_pending(), Per-conversation debounce state in Redis.  Two keys per conversation:  - ``harne, Queue a message for the conversation and (re)arm its debounce window., Remove the conversation from the due set; only one caller gets True. (+41 more)

### Community 2 - "Community 2"
Cohesion: 0.05
Nodes (40): _get_or_create_contact(), _get_or_create_conversation(), Find-or-create contact + conversation for an inbound message.  A contact is uniq, resolve_contact_and_conversation(), ingest(), IngestResult, Inbound ingestion: resolve contact/conversation, idempotently persist the messag, Queue (+32 more)

### Community 3 - "Community 3"
Cohesion: 0.06
Nodes (28): due_conversations(), Conversation ids whose debounce window has elapsed., maybe_summarize(), Rolling conversation summary: keeps the LLM's context bounded without losing old, Advance the rolling summary if enough new messages have piled up.      Never rai, _render(), _summarize(), _build_prompt() (+20 more)

### Community 4 - "Community 4"
Cohesion: 0.06
Nodes (33): _build_prompt(), _parse_score(), Frustration classifier: one best-effort LLM call scoring user frustration.  Runs, Best-effort frustration score in [0.0, 1.0], or `None` on any failure.      Neve, _recent_messages(), _score(), score_frustration(), get_redis() (+25 more)

### Community 5 - "Community 5"
Cohesion: 0.07
Nodes (35): Application settings, loaded from environment / .env., Settings, BaseSettings, _chunk_sentences(), _overlaps_any(), Reply splitting + typing-pace calculation for the humanizer (ticket 14).  Pure,, Split reply text into chat-bubble-sized parts.      1. Primary: split on blank l, Simulated typing delay for one bubble, clamped to configured bounds. (+27 more)

### Community 6 - "Community 6"
Cohesion: 0.09
Nodes (24): ContactCreate, ContactOut, ContactUpdate, MemoryCreate, MemoryOut, DeclarativeBase, MemoryService, Long-term memory: durable facts, preferences and issues the agent records about (+16 more)

### Community 7 - "Community 7"
Cohesion: 0.14
Nodes (23): ConversationOut, HandoffCreate, HandoffOut, MessageCreate, MessageOut, The warm-handoff operator brief — the newest `Handoff` row for a     conversatio, HandoffService, IllegalTransitionError (+15 more)

### Community 8 - "Community 8"
Cohesion: 0.1
Nodes (18): LLMError, LLMProvider, LLMUsage, Provider-agnostic LLM types.  Everything above this module (context builder, age, A tool as advertised to the model. `parameters` is a JSON Schema object., Any failure talking to the provider (transport, HTTP status, bad body)., The single seam between the agent runtime and any model backend., ToolSpec (+10 more)

### Community 9 - "Community 9"
Cohesion: 0.14
Nodes (20): ChannelAdapter, IncomingMessage, OutgoingMessage, EvolutionAdapter, IgnoredEvent, WhatsApp channel via Evolution API: webhook ingestion (`messages.upsert`) and ou, Raised by `parse_incoming` for payloads that should be acknowledged     (HTTP 20, Web Chat adapter: REST in, REST poll out — there is no push transport, so `send_ (+12 more)

### Community 10 - "Community 10"
Cohesion: 0.1
Nodes (16): Base, Agent, Agent model: an LLM persona bound to instructions, model config, and tools., ToolExecution model: one call an agent run made to a Tool., ToolExecution, Tool model: an HTTP business capability an agent can call., Tool, TenantScoped (+8 more)

### Community 11 - "Community 11"
Cohesion: 0.11
Nodes (23): get_adapter(), Maps `Channel.type` to its `ChannelAdapter` implementation., The SSRF guard: what a tenant-controlled tool endpoint is allowed to reach.  DNS, test_allowlist_overrides_the_ip_checks(), test_allowlist_rejects_hosts_outside_it(), test_disabled_flag_still_rejects_non_http_schemes(), test_guard_can_be_disabled_for_development(), test_host_resolving_to_both_public_and_private_is_rejected() (+15 more)

### Community 12 - "Community 12"
Cohesion: 0.13
Nodes (21): _Delivery, EchoRuntime, What `_deliver` did: the last persisted reply row, parts sent, cut short?, What a turn needs from an agent implementation: text in, text out., A runtime that persists rows of its own and so needs the turn's session., Default runtime until the LLM runtime lands — echoes the input back., SessionAwareTurnRuntime, TurnRuntime (+13 more)

### Community 13 - "Community 13"
Cohesion: 0.25
Nodes (23): _drain_jobs(), _executions(), fake_dns(), _provider(), Customer HTTP tools end-to-end: the real pipeline (ingest -> debounce -> schedul, Scoped to this conversation: the API layer commits, so rows written by     earli, `example.test` resolves to a public address; everything else fails.      respx i, Tenant + agent (wired to `get_order`) + tool row + webchat channel. (+15 more)

### Community 14 - "Community 14"
Cohesion: 0.17
Nodes (16): _drain_jobs(), _handoffs(), Sentiment-triggered escalation wired into `process_turn` (ticket 21).  Same pipe, One full turn; returns (conversation_id, runtime, adapter, scorer)., Stands in for the real adapter: records outbound calls, sends nothing., RecordingAdapter, _replies(), _run() (+8 more)

### Community 15 - "Community 15"
Cohesion: 0.22
Nodes (17): ContextBuilder, Builds the prompt transcript for one agent turn.  Short-term memory is the tail, Rehydrate the tool calls the runtime stashed in `raw_payload`., System prompt + the last `agent.context_message_limit` messages.          `tools, _tool_calls_from(), _agent(), _conversation(), _memory() (+9 more)

### Community 16 - "Community 16"
Cohesion: 0.14
Nodes (12): AgentRuntime, The agent loop: context -> LLM -> tools -> reply.  One `run_turn` call is one `A, The humanizer's prompt hint, when this conversation's channel wants it., Long-term facts folded into the system prompt (see `ContextBuilder`).          G, The provider this runtime would use for `agent`.          Exposed so `app.conver, Record the assistant's tool-call step so later turns can replay it., A `SessionAwareTurnRuntime` (see app/conversations/turn.py).      `provider` is, LLMMessage (+4 more)

### Community 17 - "Community 17"
Cohesion: 0.1
Nodes (11): failure(), NoopExecutor, Tool execution contracts.  A `ToolExecutor` is one *source* of tools, not one to, One source of callable tools., True if `execute` should be routed this tool name., Tools this executor offers to `agent` (already tenant-scoped)., Terminal fallback: advertises nothing, fails every call.      Keeps the runtime, ToolExecutor (+3 more)

### Community 18 - "Community 18"
Cohesion: 0.15
Nodes (20): _attach_contacts(), _attach_latest_handoffs(), close_conversation(), get_conversation(), _get_conversation_or_404(), handoff_conversation(), _latest_handoff(), list_conversation_messages() (+12 more)

### Community 19 - "Community 19"
Cohesion: 0.17
Nodes (15): _drain_jobs(), The humanizer wired into `process_turn` (ticket 16).  Same pipeline as `tests/te, One full turn; returns (conversation_id, adapter, events, delays)., Guards the monkeypatch above: the pacing pause must stay `asyncio.sleep`., Stands in for the real adapter: records the outbound call sequence.      `send_m, RecordingAdapter, _replies(), _run() (+7 more)

### Community 20 - "Community 20"
Cohesion: 0.26
Nodes (15): _drain_jobs(), _messages(), Human handoff: the `handoff_to_human` built-in tool end-to-end, the handoff/resu, _run_turn(), _send(), _session_factory(), _setup(), test_handoff_disabled_agent_does_not_advertise_or_execute() (+7 more)

### Community 21 - "Community 21"
Cohesion: 0.25
Nodes (14): _drain_jobs(), _message_count(), Rolling conversation summary: triggers at `agent.summary_threshold`, never touch, _run_turn(), _send(), _session_factory(), _setup(), test_maybe_summarize_carries_prior_summary_into_the_prompt_and_advances_cursor() (+6 more)

### Community 22 - "Community 22"
Cohesion: 0.19
Nodes (17): _admin_key(), _auth(), _create_channel(), _create_tenant(), Rate limiting, message size limits, safe error responses., 500 error should not leak internals., Webhook with oversized message should truncate and return 200., 31st message from same user in a minute → 429. (+9 more)

### Community 23 - "Community 23"
Cohesion: 0.25
Nodes (16): Blockers, Decisions, Next, Now, STATE — rewrite after every ticket completes; never append. ≤30 lines total., _drain_jobs(), `GET /conversations` as the operator inbox/Ativas data source: repeatable `state, _send() (+8 more)

### Community 24 - "Community 24"
Cohesion: 0.21
Nodes (14): DevMessageIn, create_tenant(), Tenant registration (admin-only) and self-lookup (per-tenant key)., TenantCreate, TenantCreated, TenantOut, ToolCreate, ToolOut (+6 more)

### Community 25 - "Community 25"
Cohesion: 0.36
Nodes (14): A single tool invocation requested by the model., ToolCall, _agent_run(), _drain_jobs(), _messages(), The agent loop end-to-end over the real pipeline: ingest -> debounce -> schedule, _run_turn(), _send() (+6 more)

### Community 26 - "Community 26"
Cohesion: 0.21
Nodes (13): ChannelCreate, ChannelOut, ChannelUpdate, create_channel(), delete_channel(), get_channel(), list_channels(), CRUD REST for channels. `secret` is write-only: encrypted on the way in via `app (+5 more)

### Community 27 - "Community 27"
Cohesion: 0.16
Nodes (12): bootstrap_chat(), BootstrapOut, chat_page(), _get_or_create_webchat_channel(), Bootstrap endpoint for the demo web chat page (ticket 13 consumes this).  No aut, Serves the self-contained demo web chat page (ticket 13)., ToolsRepository, generate_api_key() (+4 more)

### Community 28 - "Community 28"
Cohesion: 0.42
Nodes (14): _admin_key(), _auth(), _create_evolution_channel(), _create_tenant(), _fixture(), Evolution API (WhatsApp) adapter: authenticated webhook ingestion and outbound `, test_duplicate_key_id_inserts_once(), test_from_me_is_ignored_with_200_and_no_row() (+6 more)

### Community 29 - "Community 29"
Cohesion: 0.13
Nodes (14): Agent loop, Architecture, Barge-in, code:block1 (app/), code:block2 (tenants (admin-created; api_key_hash)), Data model, Extension points, Humanizer (+6 more)

### Community 30 - "Community 30"
Cohesion: 0.35
Nodes (13): _drain_jobs(), _handoffs(), RaisingProvider, The warm-handoff operator brief: `app.handoff.summarizer.handoff_with_summary` p, Always fails — proves the summarizer's LLM failure path is harmless., _run_turn(), _send(), _session_factory() (+5 more)

### Community 31 - "Community 31"
Cohesion: 0.29
Nodes (11): LLMResponse, FakeLLMProvider, Scripted provider for tests (and offline demos).  Lives in `app/` rather than `t, Returns `scripted` responses in order, recording every call.      With `repeat_l, `GET /conversations/{id}` exposes `latest_handoff` (reason, summary, requested_b, _send(), _setup(), test_conversation_without_handoff_has_null_latest_handoff() (+3 more)

### Community 32 - "Community 32"
Cohesion: 0.44
Nodes (13): _contact_id(), _drain_jobs(), Long-term memory: the `remember` built-in tool end-to-end, REST CRUD, tenant iso, _run_turn(), _send(), _session_factory(), _setup(), test_cross_tenant_cannot_read_or_delete_memory() (+5 more)

### Community 33 - "Community 33"
Cohesion: 0.29
Nodes (12): create_contact(), create_contact_memory(), delete_contact(), delete_contact_memory(), from_model(), get_contact(), _get_contact_or_404(), list_contact_memories() (+4 more)

### Community 34 - "Community 34"
Cohesion: 0.26
Nodes (11): AgentCreate, AgentOut, AgentUpdate, create_agent(), delete_agent(), get_agent(), list_agents(), CRUD REST for agents. Every route is scoped to the calling tenant. (+3 more)

### Community 35 - "Community 35"
Cohesion: 0.32
Nodes (10): _admin_key(), _auth(), _create_tenant(), CRUD REST coverage for agents, channels, tools, contacts, plus read-only convers, test_agent_crud_lifecycle(), test_channel_crud_and_secret_never_returned(), test_contact_crud_and_unique_per_channel(), test_conversations_are_read_only_and_empty_by_default() (+2 more)

### Community 36 - "Community 36"
Cohesion: 0.17
Nodes (11): API overview, Architecture (at a glance), code:block1 (┌──────────────┐        ┌───────────────┐), code:bash (cp .env.example .env), code:bash (uv run ruff check .), Conversational Agent Harness, Evolution API (WhatsApp) setup, Known MVP limitations (+3 more)

### Community 37 - "Community 37"
Cohesion: 0.18
Nodes (5): ChannelAdapter, Channel adapter interface.  Every inbound transport (Web Chat REST, WhatsApp/Evo, One implementation per `app.models.channel.ChannelType`., Mark an inbound message as read on the channel's transport.          No-op by de, Show a "typing…"/composing indicator on the channel's transport.          No-op

### Community 38 - "Community 38"
Cohesion: 0.31
Nodes (10): _get_messages(), _load_env_file(), main(), _print_transcript(), Scripted end-to-end demo against a *running* `docker compose up` stack — the PRO, Poll until the transcript grew past `baseline_count` AND the newest     message, `.env` values, with real environment variables taking priority —     mirrors pyd, _settings() (+2 more)

### Community 39 - "Community 39"
Cohesion: 0.2
Nodes (9): add_exception_handlers(), generic_exception_handler(), http_exception_handler(), Global exception handler: safe error responses with request_id., HTTPException handler: wrap in our error format., Validation errors (422) keep field detail., Generic exception handler: never leak internals., Register exception handlers on the FastAPI app. (+1 more)

### Community 40 - "Community 40"
Cohesion: 0.42
Nodes (8): create_tool(), delete_tool(), _encode_auth_config(), get_tool(), list_tools(), CRUD REST for tools. `auth_config` is write-only: encrypted on the way in via `a, _repo(), update_tool()

### Community 41 - "Community 41"
Cohesion: 0.25
Nodes (5): _ensure_test_database_exists(), _ensure_valid_secrets_key(), Shared pytest fixtures: an isolated test DB, fake Redis, and an ASGI client with, `.env`'s SECRETS_KEY is a human-readable placeholder, not a real Fernet     key., test_engine()

### Community 43 - "Community 43"
Cohesion: 0.5
Nodes (7): _admin_key(), _auth(), _create_tenant(), Cross-tenant isolation: tenant B must never see tenant A's rows, and a tool's `a, test_tenant_b_cannot_delete_or_update_tenant_a_resources(), test_tenant_b_cannot_get_tenant_a_tool_and_auth_config_never_returned(), test_tenant_b_cannot_list_or_get_tenant_a_agent()

### Community 44 - "Community 44"
Cohesion: 0.38
Nodes (6): acquire(), lock_key(), Per-conversation advisory lock (`harness:lock:{conv_id}`).  `SET key <token> NX, Return the lock token on success, `None` if someone else holds it., Release only if we still own the lock. True if it was ours., release()

### Community 45 - "Community 45"
Cohesion: 0.48
Nodes (6): Barge-in — interruption handling, Operator UI — handoff inbox, Out of scope, Primary user story, Success demo, What / why

### Community 46 - "Community 46"
Cohesion: 0.33
Nodes (5): Test the bootstrap endpoint for the demo webchat., Bootstrap creates a new channel with humanize: true., Bootstrap upgrades existing channel's config if humanize is missing., test_bootstrap_creates_channel_with_humanize(), test_bootstrap_idempotent_upgrade_humanize()

### Community 47 - "Community 47"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

### Community 48 - "Community 48"
Cohesion: 0.33
Nodes (5): Checks (run in order — cheapest first), Context, Done when, Goal, Not included

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

### Community 81 - "Community 81"
Cohesion: 0.5
Nodes (3): operator_page(), Route for the self-contained operator page (ticket 25).  Mirrors `app.api.chat`', Serves the self-contained operator inbox page (ticket 25).

## Knowledge Gaps
- **404 isolated node(s):** `handoff_summary: nullable operator-brief column on handoffs  `handoffs.summary``, `handoff_message: per-agent copy sent when a conversation flips to human handoff`, `summary_cursor: rolling-summary bookmark on conversations  `summary_cursor` is n`, `sentiment_flag: per-agent opt-in for the sentiment escalation classifier  `agent`, `agent_run_superseded: 'superseded' status for barge-in  A turn that is interrupt` (+399 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **28 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `AgentRuntime` connect `Community 16` to `Community 0`, `Community 1`, `Community 32`, `Community 2`, `Community 5`, `Community 6`, `Community 7`, `Community 8`, `Community 9`, `Community 10`, `Community 12`, `Community 13`, `Community 15`, `Community 19`, `Community 20`, `Community 21`, `Community 25`, `Community 30`?**
  _High betweenness centrality (0.095) - this node is a cross-community bridge._
- **Why does `RedisQueue` connect `Community 2` to `Community 32`, `Community 1`, `Community 4`, `Community 7`, `Community 8`, `Community 12`, `Community 13`, `Community 14`, `Community 82`, `Community 83`, `Community 19`, `Community 21`, `Community 20`, `Community 23`, `Community 24`, `Community 25`, `Community 30`?**
  _High betweenness centrality (0.090) - this node is a cross-community bridge._
- **Why does `Agent` connect `Community 10` to `Community 0`, `Community 34`, `Community 5`, `Community 6`, `Community 7`, `Community 8`, `Community 12`, `Community 14`, `Community 15`, `Community 16`, `Community 17`, `Community 82`, `Community 83`, `Community 20`, `Community 21`?**
  _High betweenness centrality (0.045) - this node is a cross-community bridge._
- **Are the 53 inferred relationships involving `AgentRuntime` (e.g. with `ContextBuilder` and `Settings`) actually correct?**
  _`AgentRuntime` has 53 INFERRED edges - model-reasoned connections that need verification._
- **Are the 50 inferred relationships involving `FakeLLMProvider` (e.g. with `LLMMessage` and `LLMResponse`) actually correct?**
  _`FakeLLMProvider` has 50 INFERRED edges - model-reasoned connections that need verification._
- **Are the 51 inferred relationships involving `LLMResponse` (e.g. with `FakeLLMProvider` and `OpenAICompatibleProvider`) actually correct?**
  _`LLMResponse` has 51 INFERRED edges - model-reasoned connections that need verification._
- **Are the 35 inferred relationships involving `RedisQueue` (e.g. with `Job` and `Queue`) actually correct?**
  _`RedisQueue` has 35 INFERRED edges - model-reasoned connections that need verification._