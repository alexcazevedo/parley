# STATE — rewrite after every ticket completes; never append. ≤30 lines total.

## Now
11 e2e + demo + README (sonnet, main)

## Next
final diff review with user (mvp approval 2 of 2)

## Decisions
- execution: parallel×2; lanes merged into main at a2e3f75; worktrees + lane branches removed
- host ports: Postgres 5434, Redis 6380, API 8001 (5433/8000 taken by other local projects)
- uv; tests: real Postgres :5434 (`harness_test`) + fakeredis; `.gitignore` `.venv/` didn't match symlink → untracked manually
- routers in app/main.py; deps app.api.deps.get_db/get_redis; fixtures client/db_session/fake_redis
- repos: BaseRepository get/list/create/update/delete, tenant_id first, flush() only, routers commit
- ingest(session, queue, incoming)->IngestResult; Job(conversation_id, message_id, attempt); keys harness:jobs/dlq
- debounce: harness:pending:{conv} LIST, harness:due ZSET, harness:lock:{conv}; MULTI/WATCH not Lua
- agent: AgentRuntime in workers/main.py; ToolRegistry([Builtin, Http]); terminal tool via ToolResult.meta {terminal, final_text}
- capped run → AgentRun.status=ERROR error="max_iterations" (no enum member)
- Evolution: POST /webhooks/whatsapp/{channel_id}, header `apikey`
- hardening: RATE_LIMIT_*_PER_MIN, MAX_MESSAGE_CHARS; ssrf env TOOL_ALLOWED_HOSTS/TOOL_SSRF_DISABLED (promoted in 11)
- migrations 0001–0004; done 01–10, all 1 attempt each; main suite 101 passed

## Blockers
- none
