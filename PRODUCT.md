---
mode: production               # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: human                   # human | agent (set by forge:auto)
---

# No repeated side effects, no held connections (PRD epic E3, part 2)

## What / why

Part 1 made a turn safe to run again. Running it again still repeats
whatever its tools did, a turn still holds a database connection while it
waits on the model, and every outbound call opens a new HTTP client and
gives up on the first 429 or 5xx. This run fixes the three remaining
items of PRD epic E3. Every item starts with a test that fails on the
current code; an item whose reproduction fails is dropped and reported,
not "fixed" blind.

In scope (existing repo):

- **E3.6 No database transaction across slow work.** A turn does not
  hold a transaction or a connection while it calls the model, a tool,
  speech-to-text or the channel, or while it sleeps. Timeouts wrap only
  the network call, never a database operation, so a timeout cannot
  leave the session unusable.
- **E3.7 Tool calls are recorded first and not repeated.** A tool call
  is saved, in its own transaction, before it runs, and its result is
  saved when it returns. The record survives a failed or superseded
  turn. When the turn runs again, a call the earlier attempt already
  made is not made again.
- **E3.8 Shared clients, short retry, token-limit name.** The API and
  the worker each own their HTTP clients for the model, speech-to-text,
  the channel and HTTP tools, and close them on shutdown. Safe calls are
  retried on 429/5xx. The name of the token-limit parameter sent to the
  model is an operator setting.

Decided in the grill:

- **Reuse the saved result.** On a re-run (a retry, or a turn merged
  with newer messages), a call with the same tool and the same arguments
  as one that already returned gets the saved result. This applies to
  every tool and only within re-runs of the same turn, never to later
  turns. Tenants configure nothing.
- **Unknown outcome: repeat only read-only calls.** A call that was
  started and has no saved result (worker died, call timed out) is
  called again only if it is an HTTP tool using GET. Any other HTTP
  tool call is not repeated: the agent is told the outcome is unknown
  and the call is logged for the operator. Built-in tools (`remember`,
  `handoff_to_human`) are the exception: their effect is saved together
  with their result, so a call with no saved result did nothing and is
  run again.
- **A saved error is reused too.** A call that returned an error counts
  as already made.
- **Automatic retry only where a repeat is safe.** Model calls,
  speech-to-text and GET tools are retried a bounded number of times
  with a growing wait, honouring `Retry-After`. Non-GET tools and sends
  to the channel are not retried at this level and keep today's
  handling.
- **Token-limit name is one operator setting**, not per agent: there is
  one model endpoint per deployment. Default stays `max_tokens`.
- The README and architecture doc describe the new behaviour and
  settings; `docs/FOLLOW_UPS.md` drops what this run closes.

## Primary user story

As someone running parley for my customers, I know that a retried or
interrupted turn does not book, charge or notify twice, that slow model
calls do not use up my database connections, and that one rate-limit
response from a provider does not fail a customer's turn.

## Out of scope

- A per-tool "safe to repeat" setting; treating non-GET tools as
  repeatable
- Retrying sends to the channel or non-GET tools on 429/5xx
- Exactly-once delivery of replies (the duplicate reply after a crash
  between saving and finishing, from part 1)
- An operator page or API for unknown-outcome calls or the dead-letter
  list
- Retrying a turn whose model call failed after the short retry is
  exhausted (the runtime's own error text stays)
- Raising the DB pool size; the reply-check token limit; DNS-rebinding
  pin; E2.6
- PRD epics E4–E7 and the rest of `docs/FOLLOW_UPS.md`

## Success demo

Done when the test suite proves that no database transaction is open
while the model or a tool is being called, a timed-out model call leaves
the session usable, a retried turn does not call a tool again that
already returned and gets its saved result, a non-GET call with unknown
outcome is not repeated while a GET one is, a tool call's record
survives a failed turn, a model call answered with 429 then 200
succeeds without failing the turn, a non-GET tool answered with 503 is
called exactly once, clients are created once and closed on shutdown —
and the existing suite still passes.
