---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: human                   # human | agent (set by comb:auto)
---

# Project-owned channel (plug and play, milestone 1 of 5)

## What / why

Parley can talk to customers only through channels it owns: its web chat
(text, replies polled) and a WhatsApp number it connects to itself. A
project that already has its own inbox, staff and customer channel cannot
use it. The first adopter, vendezap (a store's shared WhatsApp inbox), owns
its numbers, its Conversas screen and its attendants. A clinic's reception
system or a law firm's intake tool is in the same position.

This milestone adds a channel the project owns: the project forwards what
its customers and staff say, and parley pushes back what the agent does.

## Primary user story

As a developer (or an AI agent) integrating a business that already has an
inbox, I follow one guide, forward my messages to parley, expose one URL,
and my customers are answered by the agent inside my own inbox, with my
staff able to take over at any moment.

## In scope

**Ownership.** The project's inbox is the source of truth and its staff
are the only operators. Parley keeps a copy so the agent has context.
Parley's waiting lines and operator page are not used by such a project.
One tenant per installation of the project.

**Messages in.** The project forwards every customer message and every
message its staff sends. Customer messages may be text, image, voice note
or PDF. Images and voice notes behave as on parley's WhatsApp channel
today. PDFs are new: their text is given to the agent; a PDF without
readable text, or over the size limit, gets the agent's standard
"unsupported" answer. Agent replies are text.

**Agent on and off.** The agent answers until it hands off by itself or a
staff message arrives; either pauses it for that conversation. The project
calls "hand back to agent" to resume. No automatic resume.

**Departments.** Defined in parley. When the agent picks one, the project
is told its id and name and shows it its own way (vendezap: an etiqueta of
the same name). When staff changes it in the project, the project tells
parley, and the agent stops deciding for that conversation.

**Events out.** Parley pushes agent replies, handoffs (with the brief),
department changes and agent on/off changes to a URL of the project.
Pushes are signed with a secret parley generates and shows once, and are
retried for a limited time. Every event is also kept in an ordered feed
the project reads from its last position, so a long outage loses nothing.
Events carry an id; a repeat must be harmless.

**Proof and docs.** A small example project in this repo (its own tiny
inbox) used by the tests and by a fresh-agent run. A guide,
`docs/guides/project-channel.md`, linked from `llms.txt`, passing the
drift and replay tests. A brief, `docs/integrations/vendezap.md`, ready to
paste into `/comb:grill` in the vendezap repo: what vendezap must send,
receive and show, mapped to its own code.

## Out of scope

Later milestones; this one must leave room for each:
- **One-step setup** (one file, applied idempotently): the channel and its
  push URL must be creatable by plain data, no manual step.
- **Knowledge base**: nothing here; replies stay text.
- **Business functions as tools**: the existing HTTP tools are unchanged.
- **Admin UI**: every action here is a tenant-scoped endpoint.

Never: changes in the vendezap repo (its own run), billing, marketplace,
hosted SaaS, per-language SDKs, media in agent replies, reading scanned
PDFs, automatic resume of the agent.

## Constraints

- Existing endpoints and channels keep working unchanged.
- Everything is tenant-scoped.
- Nothing is specific to retail: the guide shows the same flow for a
  clinic in one paragraph.

## Success demo

Done when a fresh agent, given only `llms.txt`, connects the example
project to an empty parley stack running the stub model, and: a customer
message is answered inside the example inbox by push; a staff message
pauses the agent; "hand back" resumes it; the department chosen by the
agent appears in the example inbox; and after the example project is
stopped and restarted, the replies it missed arrive from the feed, each
once.

## Grill log

| # | Question | Answer |
|---|---|---|
| 1 | Mode | mvp |
| 2 | Source of truth for conversations and operators | The project |
| 3 | Who turns the agent off and on | A staff message pauses; explicit hand back resumes |
| 4 | Departments | Parley decides, project maps; staff changes are sent back |
| 5 | Project down | Retries, then an ordered catch-up feed |
| 6 | Where the milestone stops | Parley plus an example project, and a vendezap brief |
| 7 | Media | Text, image, voice note and PDF in; text out |
