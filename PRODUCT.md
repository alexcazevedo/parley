---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: human                   # human | agent (set by comb:auto)
---

# Session memory: short term and long term (milestone 7)

## What / why

A conversation is reused until someone closes it, and vendezap never closes
one. So a customer who writes today continues the conversation of a month
ago: the agent reads the last 20 messages whatever their age, plus one
summary of the whole history that only grows. Lasting facts are stored only
when the model happens to call `remember`.

This milestone makes the agent remember yesterday in full, last month as a
short recap, and the person's lasting facts always.

## Primary user story

As a shop whose customers come back, I want the agent to continue a chat
from yesterday as if it never stopped, greet a customer who returns after a
month without dragging in last month's details, and still know who they are
and what they usually want.

## In scope

**Sessions.** A session is a run of messages with no silence longer than a
gap, 48 hours by default, counted from the last message of either side.
Inside a session nothing changes: the agent reads the recent messages and a
running summary, which no longer spans sessions.

**When a session ends**, by silence or because the app closed the
conversation (`POST /conversations/{conversation_id}/close`, which exists
today), parley does the same two things:
- writes a short dated recap of the session;
- extracts lasting facts about the person and merges them with what is
  known: a repeated fact is not duplicated, a contradicted fact is replaced.

**What a new session reads.** No message of an earlier session. The
person's facts, at most the 20 most important as today. The most recent
recaps: at most 3, none older than 90 days. Older recaps stay stored but
leave the prompt.

**The `remember` tool stays.**

**Settings per agent**: the gap, the number of recaps, their maximum age,
and a switch to turn the whole behaviour off.

**On by default.** Conversations already running are not rewritten: the
next customer message after the gap starts a new session, and the existing
summary becomes its recap.

**Visible.** Recaps are listed by the existing contact memories API and
appear in the handoff brief.

**Docs.** Agent-behaviour, setup, project-channel.

## Out of scope

- Remembering business state, such as which pedido a chat is about: that
  stays with the app, read through its tools.
- Any change in vendezap. Calling close when a pedido is finished is a
  follow-up for that repository.
- Searching old messages on demand.
- Facts that expire by age.
- Settings per customer.
- A new screen or a new event.

## Constraints

- Switched off, an agent behaves exactly as today.
- Ending a session costs at most one extra model call and never delays a
  reply.
- Customer text is data, never instructions. No stored message is deleted.

## Success demo

Done when, with one customer on an external channel: they chat today and
again tomorrow and the agent still sees yesterday's messages; they write
after more than 48 hours of silence and the prompt holds no message of the
old session but does hold its dated recap and the facts learned from it;
after several sessions only the 3 most recent recaps are in the prompt and
one older than 90 days is not; a fact contradicted in a later session is
replaced, not duplicated; an app closing the conversation produces the same
recap and facts as a session ended by silence; switched off, an agent
behaves as today; and a returning customer's prompt is smaller than today's
for the same history.

## Grill log

| # | Question | Answer |
|---|---|---|
| 1 | Mode | mvp |
| 2 | Where does it live: parley, vendezap or both? | Both: parley ages memory by itself, and an app's explicit close counts as the same kind of ending. Which pedido a chat belongs to is business state and stays with vendezap's tools |
| 3 | When does the current conversation end by itself? | After a gap of silence, 48 hours by default, per agent |
| 4 | What is kept from an ended session? | A short dated recap plus lasting facts; the prompt shows at most 3 recaps, none older than 90 days |
| 5 | Who writes the facts, and do they age? | Written automatically when a session ends, merged and replaced on contradiction; `remember` stays; no expiry by age; at most 20 read |
| 6 | On for every agent or opt-in? | On by default; an agent can change the numbers or switch it off |
| 7 | Is the success demo right? | Yes, as written above |
