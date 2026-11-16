---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: human                   # human | agent (set by forge:auto)
---

# Agent boundaries

## What / why

Today only the free-text `instructions` field (the system prompt)
keeps an agent on its job, and nothing checks what the agent says. A
project that integrates parley has no dependable place to say "this
agent talks about X, never about Y", and no way to know the limit held.

This run adds boundaries as a first-class part of an agent, in two
layers, and enforces them on every reply.

Boundaries cover three things:

- **Topic scope.** What the agent is for. Anything outside it is
  declined (a clinic agent does not write code or discuss politics).
- **Never-list.** Subjects and claims the agent must not produce (no
  medical or legal advice, no price promises, no talk about
  competitors).
- **Staying in role.** A user who says "ignore your instructions" does
  not get a different agent, and the instructions are not revealed.

Two layers:

- **Per agent, set by the tenant** through the existing agent create
  and update API, next to `instructions`: scope, never-list, refusal
  message.
- **Operator floor, set on the server.** It applies to every agent of
  every tenant. A tenant can add limits to it and can never loosen or
  remove it.

Enforcement:

- The boundaries that apply to an agent (floor plus its own) are put
  into the system prompt in fixed wording, so the model is told the
  same way every time.
- Every agent reply is checked against those boundaries before it is
  sent. A reply that breaks them is not sent. The customer gets the
  agent's refusal message instead (a default text if the agent sets
  none) and the conversation carries on.
- If the check itself fails (model error, timeout), the reply is
  treated as blocked. A boundary does not fail open.
- Each blocked reply is recorded with the reason, so the operator can
  see what was stopped and why.
- A tenant may switch the reply check off for an agent to save the
  extra model call; that agent then relies on the prompt alone. The
  switch has no effect on the operator floor: when a floor is set, its
  check always runs.
- An agent with no boundaries, on a server with no floor, behaves
  exactly as today: no extra call, no change in replies.

## Primary user story

As a developer integrating parley into my product, I describe once
what my agent is allowed to talk about and what it must never say, and
from then on no reply outside those limits reaches my customers, even
when a customer tries to talk the agent out of them.

## Out of scope

- Screening the customer's message before the agent runs
- Asking the agent to try again before refusing
- Handing off to a human after repeated blocked replies
- Limits on tools or actions (an agent already only has the tools
  attached to it)
- Boundaries shared across all agents of one tenant
- Checking replies written by human operators
- Operator page screens for editing boundaries or browsing blocked
  replies (API and logs only)
- Measuring how often a real model obeys; tests use a scripted model
- The parked security follow-up run (`tickets/parked-followups/`) and
  PRD epics E3–E7

## Success demo

Done when I can create an agent with a scope and a never-list, send it
a message that makes the scripted model answer outside them, and see
the refusal message delivered instead of that answer with the block
recorded, while an in-bounds answer is delivered unchanged, an operator
floor still blocks on an agent that set no boundaries of its own, and
an agent with no boundaries makes no extra model call.
