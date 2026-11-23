---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: human                   # human | agent (set by comb:auto)
---

# API surface and performance (PRD epic E5)

## What / why

The API accepts values that break a turn later (a temperature of 50, a
tool named like a built-in), hides two agent settings that only the demo
bootstrap can set, and reads whole tables to answer a list or a poll.
This run closes PRD epic E5: the API refuses bad input, exposes what it
stores, and every list and poll costs one bounded, indexed query.

In scope (existing repo):

- **Two hidden agent settings are exposed.** `sentiment_escalation_enabled`
  and `handoff_message` can be set and read through `/agents`.
- **Bad input is refused with 422.** Bounds on the agent's numbers
  (`temperature`, `max_tokens`, `max_tool_iterations`,
  `context_message_limit`), on a tool's `method`, `timeout_seconds` and
  name format, and on the length of an operator's message. A tool cannot
  take the name of a built-in (`remember`, `handoff_to_human`).
- **The message poll asks only for what is new.** Messages carry their
  order number (`seq`); `?after_seq=` returns only later ones, filtered
  in the database. Without it the endpoint returns the latest page.
- **Every list endpoint is paged.** `?limit=` (default 100, max 500) and
  `?cursor=`; the body stays a plain JSON array and the token for the
  next page comes in the `X-Next-Cursor` response header. Existing
  callers keep working.
- **Missing indexes are added** for the lookups the turn and the operator
  inbox make on every request.
- **Reading memories no longer writes.** The agent gets a contact's
  memories by importance, then newest first. `remember` and the memories
  API do not store the same fact twice. The number of memories given to
  the agent becomes a setting (default 20).

Decided in the grill:

- Header cursor, not an envelope: no existing caller breaks.
- Lists that returned everything now return at most 100 rows unless
  `limit` says otherwise. This is the one visible change.
- The access write is dropped, not batched: stamping the top 20 on every
  turn kept newer memories out for ever.
- `messages(conversation_id, created_at)` from the PRD is not added: the
  poll runs on `seq`, which is already indexed, and `agent_runs` already
  has its index.
- The README, architecture doc and `docs/FOLLOW_UPS.md` describe the
  changes.

## Primary user story

As a developer building on parley, I get a clear 422 when I send a value
the harness cannot run with, I can page through any list, and my chat
screen polls for new messages without the server re-reading the whole
conversation each time.

## Out of scope

- Changing the `/chat` and `/operator` demo pages to poll with
  `after_seq` (they keep loading the latest page)
- Paging `GET /lines` and `GET /contacts/{id}/memories` (both already
  take a capped `limit`)
- Checking stored rows against the new bounds; a migration for them
- Semantic memory search, memory pruning or decay
- Rate limits, response caching
- PRD epics E6 and E7 and the rest of `docs/FOLLOW_UPS.md`

## Success demo

Done when I can create an agent with `sentiment_escalation_enabled` and
a `handoff_message` and read them back, get a 422 for a temperature of
50 and for a tool named `remember`, page through a list of agents two at
a time using `X-Next-Cursor` until the header is absent, and poll a
conversation with `after_seq` and receive only the messages stored since
— and the test suite proves the same, plus that the poll issues one
query on `messages` filtered in SQL, that storing the same memory twice
keeps one row, that reading memories writes nothing, and that the
existing suite still passes.
