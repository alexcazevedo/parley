# PRD — Hardening backlog from the 2026-11-15 codebase review

All seven epics below shipped; this file is kept as a record.

Input brief for `/forge:idea`. One forge run cannot carry all of this:
the work is split into **seven epics, each sized for one run**. Start a
run with e.g. `/forge:idea epic E1 from PRD.md`; forge should treat the
epic's section as answered questions and grill only its "Open questions".

## Context

- Source: full read of `app/` (~5.5k LOC), migrations, docs and static
  pages at commit `d2ff63d`. `ruff check` is clean.
- **Evidence level: code reading only.** The test suite was not run
  (local Postgres/Redis were down) and nothing was exploited live. Each
  item should start with a failing test that reproduces it; if a
  reproduction fails, drop or re-scope the item.
- File/line references are as of `d2ff63d` and may drift.
- Items already listed in `docs/FOLLOW_UPS.md` are not repeated unless
  the review added something.

## Goals

1. A fresh clone installs, imports and passes tests.
2. No path lets one tenant, an end user or an unauthenticated caller
   read or write another tenant's data, or spend the LLM budget.
3. An accepted inbound message always ends in a reply, a handoff or a
   visible failure — never silence.
4. One slow conversation does not stall the others.
5. Every regression above is caught by CI.

## Non-goals

- New product features (outbound media, SSE/WebSocket, semantic memory,
  operator accounts/roles) — see `docs/FOLLOW_UPS.md`.
- Replacing Redis lists with another broker.
- Per-tenant LLM keys and cost accounting (noted in E7 as a candidate
  only).

## Suggested order

E1 → E2 → E3 → E4 → E5 → E6 → E7. E1 blocks everything. E2 and E3 are
independent of each other. E6 (CI) can be pulled forward at any time.

---

## E1 — Repo is installable (blocker)

Suggested mode: prototype. Tiny, mechanical.

| ID | Problem | Requirement |
|---|---|---|
| E1.1 | `.gitignore:16` has `media/`, which also matches `app/media/`. The package (`__init__`, `prepare`, `storage`, `stt`) was never committed; `git log -- app/media` is empty. A fresh clone raises `ImportError` from `app/api/media.py`, `app/messages/ingest.py`, `app/conversations/turn.py`, `app/agents/runtime.py`. | Anchor the rule as `/media/` and commit `app/media/`. |
| E1.2 | Nothing catches this class of mistake. | Add a check that fails when a module imported by `app` is untracked (simplest: CI runs on a clean checkout — see E6). |

Done when: `git clone` into an empty directory, `uv sync`, then
`python -c "import app.main"` succeeds.

---

## E2 — Close the security holes

Suggested mode: production (security-sensitive; warrants the reviewer
pass).

| ID | Problem | Requirement |
|---|---|---|
| E2.1 | **Stored XSS in the operator page.** `app/static/operator.html:~303-310, 329` concatenates `latest_handoff.reason` and `summary` into `innerHTML`. Both are model output shaped by customer text; a script there can read the tenant API key from `localStorage`. | Render with `textContent`/DOM nodes like the rest of the page. Test with a `<img onerror>` payload in a handoff summary. |
| E2.2 | **Unauthenticated demo bootstrap.** `POST /chat/bootstrap` (`app/api/chat.py:124`) is always mounted and returns a working tenant key (`webchat_demo_api_key` default `demo-webchat-key`, `app/config.py:32`, also prefilled in `operator.html:125`). The `/dev` router is always mounted too (`app/main.py`). | Mount demo and `/dev` routes only when an explicit setting (e.g. `DEMO_MODE=true`) is on; default off. No prefilled key in static pages outside demo mode. |
| E2.3 | **Placeholder secrets boot silently.** `admin_api_key="changeme-admin-key"`, placeholder `secrets_key` (`app/config.py:13-14`). | Refuse to start with placeholder values unless demo mode is on. |
| E2.4 | **Webhook tenant/channel override.** `app/api/webhooks.py:54-58` builds `{"tenant_id":…, "channel_id":…, **payload}` so payload keys win; `EvolutionAdapter.parse_incoming` (`app/channels/evolution.py:107-108`) trusts them. A holder of any valid channel secret can inject messages into another tenant's channel. | Trusted keys must override the payload (or be passed out-of-band). Test: payload carrying a foreign `tenant_id` is ingested under the authenticated channel. |
| E2.5 | **Cross-tenant foreign keys unchecked.** `ChannelCreate.agent_id` / `ChannelUpdate.agent_id` (`app/api/channels.py:58-62`) and `ContactCreate.channel_id` (`app/api/contacts.py:55`) are not checked against the caller's tenant; a channel can run another tenant's agent. A nonexistent id surfaces as a misleading 409. | Validate every referenced id belongs to the caller's tenant; return 404/422 otherwise. Audit all create/update schemas for the same pattern. |
| E2.6 | **Webchat widget holds the full tenant key**, and `user_id` is client-supplied. Any chat user can read all conversations and edit agents/tools. | Issue a short-lived, end-user-scoped token (one channel, one contact, send + read own conversation only). Tenant key never reaches the browser. |
| E2.7 | **Upload read before size check.** `app/api/webchat.py:169` does `await file.read()`; `media_max_bytes` is only enforced later in ingest. Mime type is taken from the client header. | Enforce the cap while streaming (reject at `media_max_bytes + 1`); sniff the type from content. |
| E2.8 | **SSRF gaps.** `app/tools/ssrf.py:53-66` lets CGNAT `100.64.0.0/10` through; NAT64 is blocked only incidentally. The endpoint is not validated at tool create (`app/api/tools.py`). `ssrf.py` reads `os.environ`, so values set only in `.env` do not apply. | Block on `not ip.is_global`; validate at create/update as well as at call time; read config through `Settings`. (DNS-rebinding pin stays in FOLLOW_UPS unless pulled in.) |
| E2.9 | **Unbounded JSON tool responses.** `app/tools/http.py:127-132` caps only non-JSON bodies at 8 KB; JSON goes whole into the prompt and DB. | One byte cap for all response types, applied while reading. |
| E2.10 | **Tenant rate limit never applied.** `rate_limit_tenant` / `rate_limit_user` in `app/security/ratelimit.py` are unused; only per-user `check_rate_limit` runs. INCR+EXPIRE is not atomic. A 429 on the webhook drops the Evolution message. | Apply the tenant limit; make the counter atomic; decide webhook behaviour on limit (see open questions). |

Open questions for the grill:

- E2.6: is the end-user token in scope for this run, or is "webchat is
  demo-only, documented as such" acceptable for now?
- E2.10: when a WhatsApp sender is rate-limited, queue the message late
  or drop it with a log line?

Done when: a test per item fails on `d2ff63d` and passes after; with
default settings, `/chat/bootstrap` and `/dev/*` return 404 and the app
refuses to boot on placeholder secrets.

---

## E3 — No lost messages, no stalled workers

Suggested mode: production. Touches the queue, lock and scheduler; needs
care and concurrency tests.

| ID | Problem | Requirement |
|---|---|---|
| E3.1 | **Job lost on crash.** `RedisQueue.dequeue` uses `BRPOP` and only afterwards `push_pending` (`app/queue/redis_queue.py:24`); docs describe it as "BRPOPLPUSH-alike". | Atomic move to a processing list (`BLMOVE` or Lua), with a reaper that requeues entries from dead workers. |
| E3.2 | **Turn failure drops the batch.** Any exception in `process_turn` (e.g. `adapter.send_message` raising on an Evolution outage, `app/conversations/turn.py:324`) loses the drained batch: no retry, no reply, and the re-arm at `app/workers/scheduler.py:109` is skipped so later pending messages are stranded until the next inbound. `queue.fail()` and the DLQ are never called. | On failure: requeue the batch with bounded retries and backoff, then DLQ; always re-arm if pending exists; send the configured error message once retries are exhausted. |
| E3.3 | **Claim-then-crash strands pending.** The scheduler removes the conversation from the due set (ZREM) before draining. | Make claim recoverable (visibility timeout or reaper). |
| E3.4 | **Head-of-line blocking.** `app/workers/scheduler.py:65-110` awaits one `process_turn` at a time; LLM calls plus humanizer sleeps (up to 4 parts × 4 s) stall every other conversation on that worker. | Run each claimed conversation as a task under a configurable semaphore; graceful shutdown waits for in-flight turns. |
| E3.5 | **Lock expires mid-turn.** TTL 60 s, no renewal (`app/conversations/lock.py:19`), while a turn can take STT 20 s + sentiment 5 s + LLM loop 60 s + handoff summary 10 s + paced bubbles. | Renew the lock while the turn runs (heartbeat) and verify ownership before sending/committing. |
| E3.6 | **DB transaction held across the turn**, including LLM calls and sleeps; `asyncio.wait_for` / `asyncio.timeout` wrap session operations (`score_frustration`, `generate_handoff_summary`, `run_turn`), so a cancellation mid-query can leave the session unusable. | Keep network calls and sleeps outside DB transactions; timeouts wrap only the LLM/HTTP call. |
| E3.7 | **Tool side effects repeat or orphan.** A superseded turn already executed its tools; the merged retry may call them again. On an exception, the `AgentRun`/tool rows roll back although the side effects happened. | Persist tool-call rows before execution in their own transaction; on re-run, reuse results of calls already made in the superseded run for the same arguments. |
| E3.8 | **LLM/HTTP client churn.** `get_provider` (`app/llm/factory.py:22`) builds a new provider and `httpx.AsyncClient` per call, never closed; Evolution, STT and the HTTP tool also create a client per call. No retry on 429/5xx. `max_tokens` is rejected by newer OpenAI models. | Shared clients owned by app/worker lifespan and closed on shutdown; bounded retry with backoff on 429/5xx; configurable token-limit parameter name. |

Open questions for the grill:

- E3.4: default concurrency per worker?
- E3.7: is result reuse on re-run acceptable, or should tools declare
  themselves idempotent/non-idempotent?

Done when: tests show (a) a worker killed between dequeue and
processing does not lose the job, (b) a failing send is retried and
ends in the DLQ plus an error reply, (c) two slow conversations progress
concurrently on one worker, (d) a turn longer than the lock TTL never
runs twice in parallel.

---

## E4 — Conversation correctness

Suggested mode: mvp.

| ID | Problem | Requirement |
|---|---|---|
| E4.1 | **Handoff swallowed by barge-in.** `handoff_to_human` sets `HUMAN_HANDOFF` inside `run_turn`; checkpoint 1 (`app/conversations/turn.py:200-201`) can then supersede the turn, so `handoff_message` is never sent and the requeued batch is skipped as not `ACTIVE_AI`. The sentiment path is already exempt. | Never supersede a turn whose state changed to handoff; deliver the handoff message. |
| E4.2 | **Operator actions race the worker.** Handoff/resume/close endpoints do not take the conversation lock; an AI reply can go out after the operator took over. | Operator state changes take the lock or set a flag the turn checks before each send. |
| E4.3 | **WhatsApp groups and status.** `app/channels/evolution.py:92-93` strips only `@s.whatsapp.net`; `@g.us`, `status@broadcast` and `@lid` are ingested and answered. | Ignore non-user JIDs (`IgnoredEvent`); decide `@lid` handling (open question). |
| E4.4 | **Webhook does heavy work inline.** Evolution media is fetched in the webhook request (20 s) before dedupe; base64 payloads are stored in `raw_payload`. | Dedupe first; fetch media in the worker or after acknowledging; strip base64 from stored payloads. |
| E4.5 | **Get-or-create races.** Concurrent first messages hit the contact unique constraint → unhandled `IntegrityError` → 500 (`app/conversations/resolver.py`). No unique partial index enforces one open conversation per contact/channel. | Upsert or catch-and-reselect; add the partial unique index with a migration that resolves existing duplicates. |
| E4.6 | **Wrong run marked superseded.** `_mark_run_superseded` picks the conversation's latest `AgentRun`, not this turn's. | Pass the run id. |
| E4.7 | **Sentiment prompt duplicates the batch.** History already contains it and `input_text` is appended again (`app/agents/sentiment.py:103-110`). | Include the current batch once. |
| E4.8 | **Rolling summary cursor is offset-based** (`app/conversations/summary.py`); mixed `created_at` sources (server default vs explicit `datetime.now`) can skip or repeat messages. The context window counts tool rows against `context_message_limit`. | Cursor on a monotonic key (last summarized message id/sequence); one timestamp source; decide whether tool rows count. |
| E4.9 | **Wrong channel type → 500.** `/channels/webchat/*` does not check `channel.type == WEBCHAT`; the Evolution adapter raises an uncaught `IgnoredEvent`. | Return 404/422. |
| E4.10 | **Dead validation handler.** `app/api/errors.py:66` registers pydantic `ValidationError` instead of `RequestValidationError`; 422s use the default format. | Register the right exception; test the envelope. |

Open questions for the grill:

- E4.3: treat `@lid` senders as users (newer WhatsApp privacy ids) or
  ignore until mapped to a phone number?

Done when: tests cover handoff during barge-in, operator takeover
mid-turn, group/status JIDs, and two concurrent first messages from one
user.

---

## E5 — API surface and performance

Suggested mode: mvp.

| ID | Problem | Requirement |
|---|---|---|
| E5.1 | `AgentCreate/Update/Out` (`app/api/agents.py`) omit `sentiment_escalation_enabled` and `handoff_message`; only the demo bootstrap can set them. | Expose both. |
| E5.2 | No bounds validation: `temperature`, `max_tokens`, `max_tool_iterations`, `context_message_limit`, tool `method`, `timeout_seconds`, operator `MessageCreate.content` length, tool name format and collision with builtins (`remember`, `handoff_to_human`). | Pydantic constraints on all; reject builtin name collisions. |
| E5.3 | `GET /conversations/{id}/messages` loads every message and filters `after`/tool rows in Python on each 1.5 s poll (`app/api/conversations.py:227-235`). List endpoints are unpaginated. | Filter in SQL; cursor pagination with a max page size on all list endpoints. |
| E5.4 | Missing indexes: only `tenant_id` indexes and the external-id unique index exist. | Migration adding `messages(conversation_id, created_at)`, `agent_runs(conversation_id)`, `handoffs(conversation_id)`, `memories(contact_id)`, `conversations(tenant_id, state, last_message_at)`. Confirm against `0002_core.py` first — its body was not read in the review. |
| E5.5 | `get_contact_memories` writes `last_accessed_at` every turn; `remember` has no dedupe; hard cap of 20. | Batch or drop the access write; dedupe on normalized content. |

Done when: the poll endpoint issues one indexed query returning only new
rows (asserted in a test), and invalid agent/tool payloads return 422.

---

## E6 — CI, typing, tests

Suggested mode: mvp. Can run any time after E1.

| ID | Problem | Requirement |
|---|---|---|
| E6.1 | No CI (`.github` absent). | GitHub Actions: ruff, pytest with Postgres + Redis services, on a clean checkout. |
| E6.2 | No type checker. | mypy or pyright in CI; start permissive, strict on `app/security`, `app/queue`, `app/conversations`. |
| E6.3 | Tests need live Postgres; no documented one-command setup. | `make test` (or equivalent) that brings up dependencies via compose. |
| E6.4 | No coverage for the failure modes in E2–E4. | Covered by each epic's own tests; CI keeps them. |
| E6.5 | `/health` has no readiness check. | `/ready` checking DB and Redis. |

Done when: a PR that deletes `app/media/` or reintroduces E2.4 fails CI.

---

## E7 — Ops and hygiene

Suggested mode: prototype/mvp. Low risk, mostly mechanical.

| ID | Problem | Requirement |
|---|---|---|
| E7.1 | Dockerfile runs as root; uv installed via unpinned `curl \| sh`; compose runs `alembic upgrade head` in the API start command (races with replicas); Postgres/Redis ports published with default credentials. | Non-root user; pinned uv image/version; migrations as a separate one-shot service; no published DB ports by default. |
| E7.2 | Mixed stdlib `logging` (`webhooks.py`, `evolution.py`, `webchat.py`) and structlog; the worker binds no tenant/conversation context; `X-Request-ID` accepted unvalidated. | structlog everywhere; bind `tenant_id`/`conversation_id`/`run_id` in the worker; validate or regenerate the request id. |
| E7.3 | Doc drift: queue described as BRPOPLPUSH-alike; stale "ticket 06/07" comment in `app/workers/main.py`; `parley` vs `conversational-harness` naming. | Fix docs/comments; pick one name. |
| E7.4 | Hardcoded pt-BR strings (`ERROR_MESSAGE`, `UNSUPPORTED_MEDIA_REPLY`, placeholders) and `stt_language="pt"` default. | Move to per-agent/tenant settings with the current text as default. |
| E7.5 | Inline `from app.config import get_settings` in routers; duplicated `_connection` logic in `EvolutionAdapter.send_message`. | Tidy. |
| E7.6 | Candidate, not committed: per-tenant LLM keys/base URL and token/cost accounting; basic metrics. | Decide in the grill whether to pull in or leave in FOLLOW_UPS. |

Done when: the image runs as non-root, `docker compose up` with two API
replicas migrates once, and worker logs carry conversation ids.
