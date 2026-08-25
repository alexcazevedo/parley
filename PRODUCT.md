---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
---

# Barge-in — interruption handling

## What / why

Last seamless-roadmap item (#5). When a user sends a new message while the
agent is mid-turn (LLM running, tools executing, humanizer pacing bubbles
over many seconds), today that message queues as the NEXT turn: the bot
finishes replying to the stale question, then answers the correction in a
second reply. Two bot tells at once — ignoring what the user just said,
then double-answering. A human reads the new message and adjusts.

Shape (decided during grilling):

- **Checkpoint supersede, never mid-call abort.** The turn checks "did a
  newer inbound message arrive for this conversation?" at safe points
  only: once before delivery starts, and between humanizer bubbles. No
  cancellation during LLM or tool calls — no task-abort machinery, no
  tool side-effect hazards.
- **On supersede**: unsent parts are dropped (already-sent bubbles stand —
  natural, humans get cut off mid-thought too), the turn's reply is
  abandoned, the agent run is marked superseded (observable, not an
  error), and the conversation's turn re-runs promptly with the FULL
  outstanding batch (old unanswered content + the interrupting messages)
  so the user gets one coherent reply addressing everything.
- **Guarantees** (test-carried): no inbound message is ever lost or left
  unanswered; never two replies to one batch; a conversation that is
  handed off / not ACTIVE_AI keeps today's behavior; supersede signal
  failure degrades to today's behavior (reply delivered, next turn
  answers the new message).
- Applies to all channels; webchat's polling page needs no changes.
  Detection rides the existing ingest path/queue — no new transport.

## Primary user story

As an end user who sends a correction while the bot is still typing
("actually it's order 99, not 12345"), I see it stop and give me one
answer that reflects my correction — instead of finishing a now-wrong
reply and answering me twice.

## Out of scope

- Mid-LLM / mid-tool cancellation, tool side-effect rollback
- Partial-bubble retraction (deleting already-sent messages)
- "User is typing" signals from channels as pre-emptive pause
- Operator mini-page; any webchat page changes
- Cross-conversation or cross-channel interruption semantics

## Success demo

Done when: in `/chat` I ask about order 12345 and, while the reply bubbles
are still pacing out, send "na verdade é o pedido 99" — the stale bubbles
stop and a single fresh reply addresses order 99, nothing answers twice;
plus tests covering supersede at each checkpoint, the no-lost-message and
no-double-reply guarantees, and the superseded run marking.
