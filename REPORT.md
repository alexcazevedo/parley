# REPORT: Session memory (milestone 7)

- **Vision:** the agent remembers a conversation from one or two days ago in full, an exchange from a month ago only as a short recap, and the person's lasting facts always, without overflowing the context.
- **Mode:** mvp. **Owner:** human.
- **Outcome:** done. 5 tickets finished; `make check` is green (1532 passed, 1 skipped). Committed, merged to `main` and deployed at your instruction, given before the run finished.

## Grill

Seven questions, answered by you: mvp; parley ages memory by itself and an app's close counts as the same kind of ending (which pedido a chat belongs to stays with vendezap's tools); a session ends after 48 hours of silence; an ended session leaves a dated recap and lasting facts, with at most 3 recaps under 90 days in the prompt; facts are written automatically at session end, replaced on contradiction, with no expiry; on by default; the seven-point success demo.

## Tickets

| # | ticket | model | status | attempts |
|---|---|---|---|---|
| 01 | Sessions: a message after the gap starts a new one | opus | done | 1 of 3 |
| 02 | Ending a session; recap and facts | opus | done; one existing test fixed by the orchestrator | 1 of 3 |
| 03 | What a new session reads | sonnet | done | 1 of 3 |
| 04 | Success demo | sonnet | done; found a defect, fixed by the orchestrator | 2 of 3 |
| 05 | Docs | sonnet | done | 1 of 3 |

No blocked tickets.

## The success demo

All seven points pass in `tests/test_session_memory_demo.py`. Point 7, measured: for the same history, a returning customer's prompt is **289 characters with session memory on and 3521 with it off**.

## What did not go as planned

- **Ticket 02 left one existing test red.** A worker-shutdown test starts the real worker against the shared test database; the new loop picked up sessions other tests had left pending and tried the real model (refused by the test network guard). Fixed in that test, the way it already replaces the housekeeping loop. The feature was not changed.
- **Ticket 04 found a real defect.** After the worker ended a silent session, the customer's next message created a new session and ended it again at once, leaving an empty extra session. Fixed in `begin_turn`; the demo now asserts the exact number of sessions and that none is empty.
- **Ticket 03 adjusted one test from ticket 01**, setup only: right after a gap the old running summary now stands in as a recap, by design, so that test sets the recap limit to 0. Its assertions are unchanged.
- **Docker Desktop stopped during ticket 04**, taking the test database down. It was started again and parley's test database restarted. `vendezap-db-1` also went down and was left stopped.

## How to run the result

```
uv run pytest -q -s tests/test_session_memory_demo.py
```

Per agent: `session_memory_enabled`, `session_gap_hours`, `session_recap_limit`, `session_recap_max_age_days`. Recaps: `GET /contacts/{contact_id}/memories` lists them with type `session_recap`.

## Final review: diff summary

39 files changed, 3132 insertions(+), 187 deletions(-), against `640b91b`.

**New behaviour, on by default for every agent**
- A session is a run of messages with no silence longer than the agent's gap (48 hours). A customer message after a longer silence starts a new session. Inside a session nothing changed.
- When a session ends, by silence (a worker pass) or because the app closed the conversation, one model call writes a dated recap and adds or replaces lasting facts about the person. It runs in the worker, never in the reply path; it is tried up to 3 times, then the session is marked `failed`. No stored message is touched.
- A new session reads no message of an earlier one. It reads the facts (at most 20, as before) and the most recent recaps (at most 3, none older than 90 days), under their own heading. The handoff brief gets the same.
- `remember` still works. Recaps are contact memories of the new type `session_recap`.
- New settings: `SESSION_RECAP_MODEL`, `SESSION_END_INTERVAL_SECONDS` (60).

**Where the code is**
- New: `app/conversations/sessions.py`, `app/conversations/session_end.py`, `app/models/conversation_session.py`.
- Changed: `app/conversations/turn.py`, `app/conversations/summary.py`, `app/agents/runtime.py`, `app/agents/context.py`, `app/handoff/service.py`, `app/handoff/summarizer.py`, `app/memory/service.py`, `app/repositories/memories.py`, `app/models/agent.py`, `app/models/memory.py`, `app/api/agents.py`, `app/llm/jobs.py`, `app/tools/builtin.py`, `app/workers/main.py`, `app/config.py`, `scripts/mock_llm_server.py`.
- Migrations `0031` and `0032`, both run up, down and up on scratch databases.
- Docs: the memory section of agent-behaviour rewritten; project-channel, operations, configuration, architecture, follow-ups, the vendezap brief.
- Tests: 4 new test files, about 45 tests; two existing test files touched, as described above.

**What changes for the live tenant at deploy**
- A customer who writes after more than 48 hours of silence starts a fresh session: the agent no longer reads the old messages.
- Each ended session costs one extra model call, on the agent's own model unless `SESSION_RECAP_MODEL` is set.
- A conversation that existed before the deploy has no session until its next message; from then on it follows the rules.

**Things to know**
1. **The first reply after a gap can still carry old context.** Until the worker has written the recap (about a minute), the old session's running summary stands in for it. For a long-lived existing conversation that summary covers its whole history.
2. **An agent with `memory_enabled` off still gets its recaps written, and never reads them.** One wasted model call per ended session for such agents. Not fixed in this milestone.
3. **A `failed` session is not retried.** The operations guide describes a manual reset; that reset was not tested.
4. **`POST /contacts/{contact_id}/memories` accepts the type `session_recap`**, so an app can write a recap by hand; only the model's `remember` tool is barred from it.
5. **Recaps are written by a model from customer text.** The prompt treats that text as data, and a test covers an injection attempt, but a recap is still a model's summary: it can be wrong.
6. **`docs/ARCHITECTURE.md` is at its 930-line limit.**

**Not verified**
- No real model wrote a recap: scripted fakes and the stub only. Recap quality, its language, and whether a real model follows the `ADD` / `REPLACE` format are untested.
- Behaviour under real load: many sessions ending at once.
