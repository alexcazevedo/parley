# STATE — rewrite after every ticket completes; never append. ≤30 lines total.

## Now
idle — MVP approved 2026-08-24 (mvp approval 2 of 2 spent)

## Next
- user runs `scripts/demo.py` with real LLM_API_KEY (live e2e); optional follow-ups below

## Decisions
- execution: parallel×2; lanes merged into main at a2e3f75; worktrees + lane branches removed
- host ports: Postgres 5434, Redis 6380, API 8001, mock-tools 9000 (5433/8000 taken by other local projects)
- uv; tests: real Postgres :5434 (`harness_test`) + fakeredis
- debounce: harness:pending:{conv} LIST, harness:due ZSET, harness:lock:{conv}; MULTI/WATCH not Lua
- agent: ToolRegistry([Builtin, Http]); terminal tool via ToolResult.meta {terminal, final_text}
- capped run → AgentRun.status=ERROR error="max_iterations" (no enum member)
- Evolution: POST /webhooks/whatsapp/{channel_id}, header `apikey`
- micro-fix post-11: RedisQueue.dequeue swallows redis TimeoutError on idle BRPOP (worker crashed otherwise)
- migrations 0001–0004; 12 commits; suite 102 passed; every ticket 1 attempt

## Blockers
- none (live demo needs real LLM_API_KEY in .env — placeholder now)
