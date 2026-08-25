---
id: 10
status: todo
model: haiku
depends_on: [09]
max_attempts: 3
files:
  - app/security/ratelimit.py
  - app/security/limits.py
  - app/api/errors.py
  - app/main.py
  - app/config.py
  - tests/test_hardening.py
---
## Goal
Rate limiting, message size limits, safe error responses.

## Context
`ratelimit.py`: FastAPI dependency `rate_limit(scope)` — Redis `INCR harness:rl:{scope}:{key}:{minute}` + `EXPIRE 60`; key = tenant id (management routes) or `channel_id:external_user_id` (webchat/dev/webhook). Limits from config: `RATE_LIMIT_TENANT_PER_MIN=600`, `RATE_LIMIT_USER_PER_MIN=30`. Exceed → 429.

`limits.py`: `MAX_MESSAGE_CHARS=4000` (config) enforced in ingest → 413 for webchat/dev, silently truncated + logged for webhooks (must still 200).

`errors.py`: global exception handler → `{"error": {"code", "message", "request_id"}}`; never leaks stack traces or internal exception text for 500. Validation errors 422 keep field detail.

## Checks (run in order — cheapest first)
1. `uv run ruff check .`
2. `uv run pytest -q tests/test_hardening.py`
3. `uv run pytest -q`

## Done when
- 31st message from same user in a minute → 429.
- 5000-char webchat message → 413.
- Forced exception → 500 with generic body + request_id.

## Not included
- Per-tenant configurable limits, IP-based limiting, WAF.
