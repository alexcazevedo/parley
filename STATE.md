# STATE — rewrite after every ticket completes; never append. ≤30 lines total.

## Now
warm-handoff mvp run COMPLETE 2026-08-25 — tickets 18–19 done, 1 attempt each, suite 129 green; BOTH paths live-verified (operator + agent-tool handoff produce LLM brief in latest_handoff). Awaiting final diff review (mvp approval 2 of 2).

## Next
- final diff review with user → commit

## Decisions
- summarizer wraps pure HandoffService: handoff_with_summary in app/handoff/summarizer.py; best-effort wait_for 10s, None on failure
- surface: latest_handoff {reason, summary, requested_by, created_at} on single-GET only; list endpoint deliberately excludes (N+1)
- migration 0005 handoffs.summary Text nullable; verified in compose DB
- micro-fix during run: ruff I001 tests/test_bootstrap.py (pre-existing from a7aa4c4 — ticket 17 linted app only; lesson: lint app AND tests in checks)
- prior: humanizer a7aa4c4, web chat 892a13f, harness MVP 92c2b2c
- host ports: Postgres 5434, Redis 6380, API 8001, mock-tools 9000

## Blockers
- none
