---
mode: production               # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: human                   # human | agent (set by forge:auto)
---

# Conversation correctness (PRD epic E4, part 1)

## What / why

A conversation can end up in the wrong state or get the wrong answer:
a handoff the customer never hears about, an AI reply sent after an
operator took over, replies to WhatsApp groups, a 500 on a customer's
first message. This run fixes the five items of PRD epic E4 behind
those. Every item starts with a test that fails on the current code; an
item whose reproduction fails is dropped and reported, not "fixed"
blind.

In scope (existing repo):

- **E4.1 A handoff is never swallowed by a newer message.** When a turn
  handed the conversation to a human, a customer message that arrives
  meanwhile does not cancel it: the handoff message is delivered, whole.
- **E4.2 An operator's action wins over a running turn.** Handoff,
  resume and close by an operator are saved at once and never wait for
  the turn. A running turn checks the conversation's state right before
  each send and stops when the conversation is no longer AI-driven.
- **E4.3 Only people are answered on WhatsApp.** Messages from groups
  (`@g.us`) and status broadcasts (`status@broadcast`) are ignored:
  nothing is stored and nothing is sent.
- **E4.5 First messages cannot collide.** Two first messages from the
  same person arriving at the same time produce one contact and one open
  conversation, and neither request fails. The database enforces one
  open conversation per contact and channel.
- **E4.6 The right run is marked superseded.** A superseded turn marks
  its own agent run, not whichever run is the conversation's latest.

Decided in the grill:

- **Operator wins at once.** A turn stopped by an operator's action
  sends nothing more. Bubbles already sent stay; the unsent rest is
  dropped and the stop is recorded. A tool call already in flight still
  completes. The customer gets no extra message about the cut-off.
- **Senders with a WhatsApp privacy id (`@lid`) are answered.** They are
  treated as users, keep the full `…@lid` as their id, and are replied
  to at that id. Whether Evolution delivers a send to an `@lid` is not
  verifiable in tests and needs one check against a real instance.
- **Existing duplicates: keep one, close the rest.** Before adding the
  database rule, the migration keeps, per contact and channel, the
  conversation a human is handling if there is one, otherwise the most
  recently active, and marks the others closed. Nothing is deleted and
  no messages are moved.
- The README, architecture doc and `docs/FOLLOW_UPS.md` describe the new
  behaviour and its limits.

## Primary user story

As someone running parley for my customers, I know that when a
conversation is handed to a person, the customer is told and the AI
stays quiet from that moment, that the AI never answers a group chat,
and that a customer's very first message is never lost to an error.

## Out of scope

- E4.4 Webhook fetching media inline, base64 in stored payloads
- E4.7 Sentiment prompt containing the batch twice
- E4.8 Rolling-summary cursor, timestamp sources, tool rows in the
  context window
- E4.9 Wrong channel type returning 500; E4.10 validation error format
- Merging a contact seen under both a privacy id and a phone number
- Telling the customer that a reply was cut off by an operator
- Merging or deleting duplicate conversations
- PRD epics E5–E7 and the rest of `docs/FOLLOW_UPS.md`

## Success demo

Done when the test suite proves that a handoff made during a turn is
announced to the customer even though a newer message arrived, an
operator's handoff during a paced reply stops the remaining bubbles and
no AI message follows it, a group message and a status broadcast produce
no stored message and no reply while an `@lid` sender is answered, two
simultaneous first messages from one user both succeed and leave one
contact and one open conversation, a second open conversation for the
same contact and channel is refused by the database, the migration
closes existing duplicates, a superseded turn marks its own run — and
the existing suite still passes.
