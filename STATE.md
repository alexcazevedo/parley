# STATE — rewrite after every ticket completes; never append. ≤30 lines total.

## Now
08 handoff (sonnet, lane-a worktree ../conversational-harness-lane-a)

## Next
lane A done after 08; then merge lane-a into main (expect uv.lock/pyproject conflict: lane-b added respx); then 11

## Decisions
- execution: parallel×2; lane B (09,10) merged into main at cfdbb4b, worktree removed
- host ports: Postgres 5434, Redis 6380, API 8001 (5433/8000 taken by other local projects)
- uv; tests: real Postgres :5434 (`harness_test`) + fakeredis; lane-a .venv symlinks main .venv (was corrupted once → `rm .venv && uv sync` fixes)
- routers in app/main.py; deps app.api.deps.get_db/get_redis; fixtures client/db_session/fake_redis
- repos: BaseRepository get/list/create/update/delete, tenant_id first, flush() only, routers commit
- ingest(session, queue, incoming)->IngestResult; Job(conversation_id, message_id, attempt); keys harness:jobs/dlq
- debounce: harness:pending:{conv} LIST, harness:due ZSET, harness:lock:{conv}; MULTI/WATCH not Lua
- TurnRuntime.run(conversation, input_text)->str; process_turn(session, conv_id, messages, runtime); injected in workers/main.py
- Evolution: POST /webhooks/whatsapp/{channel_id}, header `apikey`; send raises httpx.HTTPStatusError
- hardening config: RATE_LIMIT_TENANT_PER_MIN, RATE_LIMIT_USER_PER_MIN, MAX_MESSAGE_CHARS; errors via app/api/errors.py
- done: 01–03, 09, 10 on main; 04–07 on lane-a (ae642b5); all 1 attempt
- ToolExecutor: can_handle/specs(agent,session)/execute(name,args,ctx); ToolContext(session,tenant_id,conversation_id,contact_id,agent,agent_run_id); AgentRuntime._load_memories stub for 07; HttpToolExecutor.can_handle always True → builtins prepended
- MemoryService: store/get_contact_memories/search/forget (tenant_id first); BuiltinExecutor table-driven BUILTIN_TOOLS/_HANDLERS; summary_cursor = count summarized

## Blockers
- none
