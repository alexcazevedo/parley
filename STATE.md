# STATE — rewrite after every ticket completes; never append. ≤30 lines total.

## Now
humanizer mvp run COMPLETE 2026-08-25 — tickets 14–17 done, 1 attempt each, suite 121 green; live demo verified: 3 staggered bubbles + get_order round-trip at /chat. Awaiting final diff review (mvp approval 2 of 2).

## Next
- final diff review with user → then commit (both runs 12–13 + 14–17 uncommitted)

## Decisions
- humanizer: per-channel config.humanize; Settings knobs (chars_per_sec 35, delay 0.8–4s, max_parts 4); style hint appended to system prompt when humanized
- flow: mark_read (best-effort) → split (blank-line + sentence fallback, code-fence atomic) → per part typing→sleep→persist→send→commit (staggered polling bubbles); humanize off = legacy path byte-for-byte
- adapter hooks mark_read/send_typing no-op base; Evolution markMessageAsRead + sendPresence
- micro-fix post-17 (orchestrator): stale docker image — container had pre-fix in-place JSONB mutation (no UPDATE emitted); local code correct; rebuild resolved. Lesson: image rebuild must FOLLOW last code fix; grep-only checks miss it
- ticket 17 deviation kept: added tests/test_bootstrap.py (outside file list, beneficial)
- host ports: Postgres 5434, Redis 6380, API 8001, mock-tools 9000
- prior runs: harness MVP at 92c2b2c; web-chat prototype tickets 12–13

## Blockers
- none
