---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
---

# Warm-handoff summary

## What / why

When a conversation escalates to a human (`handoff_to_human` tool or
operator-initiated), today's operator gets only a raw transcript and a
one-line `reason`. Making the user repeat themselves is the classic moment
the "seamless" illusion breaks — Sierra/Decagon-class products hand the
operator an AI-written brief instead. Add that brief to the harness: at
handoff time, one extra LLM call writes a short summary the operator reads
before their first message.

Shape (decided during grilling):

- **Storage**: new nullable `summary` column on the existing `Handoff` row
  (Alembic migration). Generated for BOTH `handoff_to_human`-initiated and
  operator-initiated handoffs.
- **Surface**: `GET /conversations/{id}` gains `latest_handoff:
  {reason, summary, requested_by, created_at} | null` (newest handoff for
  the conversation). The summary NEVER enters the message transcript — the
  end-user webchat page polls messages and would leak it.
- **Content**: compact operator brief — who the contact is (known
  memories/name), what they want, what the agent already did/answered
  (including tool results), why the handoff happened, user's current tone.
  Written in the conversation's language.
- **Generation**: reuses the existing LLM provider + conversation context
  (recent messages, rolling `conversation.summary`, memories, handoff
  reason). Strictly best-effort: any LLM failure logs and completes the
  handoff with `summary = null` — escalation must never be blocked or
  delayed past a sensible timeout by the summarizer.

## Primary user story

As a human operator picking up an escalated conversation, I open it and
read a short AI-written brief — who I'm talking to, what they need, what
the bot already tried, and why it escalated — so I can send my first
message immediately without asking the user to repeat anything.

## Out of scope

- Operator dashboard/queue UI (REST only; mini-page is a future run)
- Sentiment-triggered auto-escalation (#4) and barge-in (#5)
- Re-summarizing on resume/close, or live-updating the brief while handed off
- Notifying operators (webhook/email/push) that a handoff happened
- Redaction/PII filtering inside the brief
- Changes to the webchat page or channel adapters

## Success demo

Done when: in `/chat` I say "quero falar com uma pessoa", and
`GET /conversations/{id}` returns `latest_handoff` with an LLM-written
brief covering want/tried/why/tone — plus tests proving a summary is
persisted for tool- and operator-initiated handoffs and that an LLM
failure still completes the handoff with a null summary.
