# STATE — rewrite after every ticket completes; never append. ≤30 lines total.

## Now
02 — core schema + CRUD (sonnet), dispatched

## Next
03 (sequential), then lane A 04→05→06→07→08 ∥ lane B 09→10 (worktrees), then 11

## Decisions
- execution: parallel×2 (01–03 sequential in place; lanes A/B in git worktrees; 11 after merge)
- host ports: Postgres 5434, Redis 6380, API 8001 (5433/8000 taken by other local projects)
- uv package manager; tests: real Postgres :5434 (`harness_test`) + fakeredis
- register routers in app/main.py; DB/Redis deps via app.api.deps.get_db/get_redis; fixtures client/db_session/fake_redis
- 01 done in 1 attempt, committed

## Blockers
- none
