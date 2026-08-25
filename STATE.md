# STATE — rewrite after every ticket completes; never append. ≤30 lines total.

## Now
03 — ingestion + webchat + queue (sonnet), dispatched

## Next
lane A 04→05→06→07→08 ∥ lane B 09→10 (worktrees), then 11

## Decisions
- execution: parallel×2 (01–03 sequential in place; lanes A/B in git worktrees; 11 after merge)
- host ports: Postgres 5434, Redis 6380, API 8001 (5433/8000 taken by other local projects)
- uv package manager; tests: real Postgres :5434 (`harness_test`) + fakeredis
- routers in app/main.py; deps app.api.deps.get_db/get_redis; fixtures client/db_session/fake_redis
- repos: BaseRepository get/list/create/update/delete, tenant_id first arg, flush() only, routers commit
- Message has channel_id FK; Memory.metadata → attr `meta`; enums are StrEnum
- 01, 02 done in 1 attempt each, committed (12d6d10, bbc19c5)

## Blockers
- none
