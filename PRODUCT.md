---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
---

# Humanizer — paced, split, human-feeling replies

## What / why

The harness pipeline (MVP, complete) replies correctly but *feels* like a
bot: one instant wall of text. Top-tier conversational AI (Sierra, Decagon,
Intercom Fin) makes delivery indistinguishable from a human typing: mark the
inbound message read, show "composing…", pause proportionally to how much
"typing" the reply needs, and send 2–4 short bubbles instead of one block.
Build that delivery layer — the **humanizer** — into the outbound send path
(`app/conversations/turn.py` → `ChannelAdapter.send_message`), for both real
channels.

Design decided during grilling:

- **Config**: `channel.config.humanize: bool` per channel (transport
  concern, matches existing `config` pattern). Pacing knobs are global
  `Settings` with defaults: typing speed (chars/sec), min/max per-part
  delay, max parts per reply.
- **Splitting — converged approach**: agents get one appended instruction
  line ("write short chat-style messages, blank line between them"); the
  splitter splits on blank lines, with a mechanical sentence-split fallback
  when the model returns a wall of text, hard cap ~4 parts, never mid-word,
  code blocks never split. Zero extra LLM calls.
- **Adapter surface**: optional `mark_read(...)` and `send_typing(...)`
  hooks on `ChannelAdapter`, no-op by default. Evolution implements both
  (`markMessageAsRead`, `sendPresence` composing) plus one `sendText` per
  part. Webchat implements neither — parts are persisted staggered (commit
  per part with the paced delay between), so the polling page renders
  bubbles appearing over seconds; its existing client-side typing indicator
  stays between parts.
- Humanize sequencing lives in one module (`app/conversations/humanize.py`
  or similar), called from `process_turn`; `humanize: false` (or absent)
  keeps today's single-message behavior byte-for-byte.
- Delays happen inside the conversation's turn (lock already held) — they
  only slow that one conversation, never the worker's other jobs. Failures
  in read/typing calls are best-effort: never cost the user their reply.

## Primary user story

As an end user chatting with a business on WhatsApp or web chat, I see my
message get read, then "typing…", then a couple of short, naturally paced
messages — so the conversation feels like a person, not a bot dumping a
paragraph in two seconds.

## Out of scope

- Warm-handoff summaries, sentiment-triggered escalation, barge-in/run
  cancellation (future runs — items #3–#5 of the seamless list)
- WebSocket/SSE push for webchat (polling stays)
- Per-tenant/per-agent pacing overrides beyond the global settings
- Voice notes, media, reactions; presence beyond "composing"
- Randomized "human error" simulation (typos, corrections)

## Success demo

Done when: in the browser at `/chat`, a reply that warrants it arrives as
2+ separate bubbles staggered over seconds with the typing indicator
between them; and the Evolution path is proven by tests (mocked Evolution
HTTP) asserting the exact outbound sequence — read receipt, composing
presence, then one paced `sendText` per part, in order — plus splitter unit
tests and a regression run of the full suite with `humanize` off showing
unchanged behavior.
