---
mode: production               # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: human                   # human | agent (set by forge:auto)
---

# Conversation correctness (PRD epic E4, part 2)

## What / why

Part 1 fixed who gets answered and when. This run fixes what remains of
PRD epic E4: a webhook that does slow work before it answers, a model
that sees messages in the wrong order or twice, and two API errors that
come out wrong. Every item starts with a test that fails on the current
code; an item whose reproduction fails is dropped and reported, not
"fixed" blind.

In scope (existing repo):

- **E4.4 The WhatsApp webhook answers fast.** A repeated delivery is
  recognised before any other work. Images and audio are downloaded
  after the webhook has answered, before the turn that needs them. The
  stored copy of an incoming event no longer contains base64 media.
- **E4.7 The sentiment check sees each message once.** The current
  batch is no longer in its prompt twice.
- **E4.8 Messages have one order.** Every message of a conversation has
  a position that does not depend on which clock stamped it; history,
  the rolling summary and the operator's view all use it. The rolling
  summary continues from the last message it covered, so it neither
  skips nor repeats one.
- **E4.9 A wrong channel type is a clean error.** Calling a webchat
  endpoint with a channel of another type returns a 4xx, not a 500.
- **E4.10 Validation errors use the API's error format.** A request
  that fails validation returns 422 in the same envelope as every other
  error.
- **Leftovers from part 1.** A retried handoff message uses the default
  text when the agent has none configured. No test reaches the network:
  the handoff brief is faked in tests, and a test that tries to call out
  fails.

Decided in the grill:

- **The history limit counts conversation, not tools.**
  `context_message_limit` counts customer and assistant messages; the
  tool calls and results of those turns come along and a call is never
  separated from its result. Prompts of tool-heavy agents get longer.
- **Old stored payloads stay as they are.** Only new messages are stored
  without base64. Cleaning old rows is irreversible and is left as a
  follow-up.
- **A failed download behaves as today.** The message is kept as
  "media not received" and the customer gets the existing
  unsupported-media reply.
- The README, architecture doc and `docs/FOLLOW_UPS.md` describe the new
  behaviour and its limits.

## Primary user story

As someone running parley for my customers, I know that WhatsApp never
gives up on a slow webhook, that the model reads each conversation in
the order it happened, and that a client calling the API wrongly gets an
error it can read.

## Out of scope

- Stripping base64 from payloads already stored
- A separate cap on the total size of the prompt
- Recording an operator's stop on the agent run (new run status)
- Making the sleep-based scheduler test deterministic
- Remote media storage; new media types
- PRD epics E5–E7 and the rest of `docs/FOLLOW_UPS.md`

## Success demo

Done when the test suite proves that a repeated WhatsApp delivery causes
no download and no second message, a media message is acknowledged
before its download starts and is still understood by the turn, a stored
event contains no base64, the sentiment prompt contains the batch once,
a conversation whose replies were stamped by a clock ahead of or behind
the database is still read in the order it happened, the rolling summary
covers every message exactly once across several rounds, a history limit
of N gives the model N customer and assistant messages with their tool
rows intact, a webchat call on a WhatsApp channel returns a 4xx, a
validation failure returns 422 in the error envelope, a retried handoff
message with no configured text sends the default, the suite runs with
the network blocked — and the existing suite still passes.
