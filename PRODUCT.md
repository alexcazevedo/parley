---
mode: production               # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: human                   # human | agent (set by forge:auto)
---

# No lost messages, no stalled workers (PRD epic E3, part 1)

## What / why

A customer's message can be lost today, and one slow conversation holds
up every other one on the same worker. This run fixes the five items of
PRD epic E3 that cause that. Every item starts with a test that fails on
the current code; an item whose reproduction fails is dropped and
reported, not "fixed" blind.

In scope (existing repo):

- **E3.1 Job survives a worker crash.** A job taken from the queue is
  recorded as in progress in the same step. Jobs left behind by a dead
  worker are put back.
- **E3.2 A failed turn is retried.** Any error during a turn puts the
  batch back for another attempt: up to 3 attempts, with a growing wait
  of seconds between them. After the last one the batch goes to the
  dead-letter list and the customer gets the fixed error message once.
  Messages that arrived meanwhile are always scheduled, failure or not.
- **E3.3 A claimed conversation is recoverable.** If a worker dies
  after claiming a due conversation and before finishing it, the
  conversation is picked up again after a timeout.
- **E3.4 Conversations run concurrently.** One worker runs several
  conversations at once, 4 by default, set by the operator. The same
  conversation never runs twice in parallel. On shutdown the worker
  takes no new turns and waits for the running ones.
- **E3.5 The lock holds for the whole turn.** The conversation lock is
  renewed while the turn runs. A turn that finds it no longer owns the
  lock stops before sending or saving anything further, and its batch is
  put back for the current holder.

Decided in the grill:

- **Retry is the whole turn**, including the model call. Tools that ran
  in a failed attempt may run again; preventing that is E3.7, a later
  run.
- **No retry once anything reached the customer.** If part of a reply
  was delivered before the failure, the turn ends there: what was sent
  stays, the rest is dropped, the batch goes to the dead-letter list
  marked as partly delivered, and no error message is added.
- **The dead-letter list** is logged and readable in Redis. No API or
  page for it in this run.
- The README and architecture doc describe the new behaviour and the
  concurrency setting; the wrong "BRPOPLPUSH-alike" claim is corrected.

## Primary user story

As someone running parley for my customers, I know that a message a
customer sent is answered or ends up where I can see it, even when a
worker crashes or a provider is down, and that one slow conversation
does not make everyone else wait.

## Out of scope

- E3.6 DB transaction held across model calls and sleeps
- E3.7 Repeated or orphaned tool side effects
- E3.8 Shared HTTP clients, retry on 429/5xx, token-limit parameter name
- Resending only the undelivered bubbles of a partly delivered reply
- An API or operator page for the dead-letter list; replaying it
- Raising the default DB pool size; multi-worker autoscaling
- PRD epics E4–E7 and everything in `docs/FOLLOW_UPS.md`

## Success demo

Done when the test suite proves that a worker killed between taking a
job and processing it does not lose the job, a turn whose send keeps
failing is retried and ends in the dead-letter list with one error
reply, a partly delivered reply is not sent again, two slow
conversations progress at the same time on one worker, and a turn longer
than the lock's lifetime never runs twice in parallel — and the existing
suite still passes.
