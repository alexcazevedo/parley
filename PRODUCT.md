---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: agent                   # human | agent (set by comb:auto)
---

# A model per job (milestone 5)

The brief is `docs/roadmap/2-model-per-job.md`; the decisions common to the
roadmap are in `docs/roadmap/README.md`. This file is the short form.

## What / why

A turn makes up to five model calls besides the reply: the boundary check,
sentiment, the department, the rolling summary and the handoff brief. All
run on the agent's own model, the most expensive and slowest choice for a
yes-or-no answer. This milestone lets the server owner give each job its own
model, and run the three decisions on TypeSafe's decision model (Jev), which
answers typed questions instead of writing text.

## Primary user story

As the owner of a parley server, I set one environment variable per job and
the boundary check, sentiment and department run on a small decision model,
while replies stay on the agent's model; I can read from the data which
model served each call and what it cost, and compare a model's boundary
decisions with the recorded ones before I trust it.

## In scope

**A setting per job**, in the environment, the same for every tenant; unset
means the agent's own model, exactly as today. A value is either another
chat model on the configured endpoint, or TypeSafe's decision model (with
`TYPESAFE_API_KEY`). A wrong value stops the server at start with a message
naming the setting; so does a writing job pointed at a decision model.

**Decisions on TypeSafe**, through Pydantic AI's own support for it, with
typed answers:
- Boundary check: allowed or not, and which rule was broken (the scope, one
  of the `never` items, staying in role, or none). The recorded reason is
  built from that pick; no free text, no second call.
- Sentiment: a frustration scale, mapped onto the existing threshold.
- Department: one of the tenant's departments, or none.

**When the decision model cannot take a call** (more options than it
accepts, an image, an unsure answer), that call goes to the agent's own
model and the hand-over is recorded. An error or a timeout does not hand
over: the job fails as today. The boundary check blocks the reply; sentiment
and department do nothing.

**Recorded.** One row per job call: the job, the model that served it, token
use, latency, the outcome, whether it was handed over.

**Compared before trusted.** A command replays a tenant's stored replies
through the boundary check on a candidate model and prints where it
disagrees with what was recorded.

**Docs.** Configuration (the settings, and what each job sends to TypeSafe),
agent behaviour (the jobs and what a smaller model risks), operations (the
command, reading the recorded calls).

## Out of scope

Typed answers on a chat model (they come with native providers, brief 3;
a chat model set for a job answers in today's text form). Moving the reply
to another provider. Streaming. Settings per tenant or per agent. A
pre-check that skips the model for simple messages. A free-text reason from
a second call. Comparing sentiment or department decisions (their inputs are
not stored). Changing what any job decides or its thresholds. Any framework
owning the loop.

## Constraints

- With none of the new settings, every existing test passes unchanged.
- Pydantic AI is used only as the model layer for the decision jobs.
- No test calls TypeSafe or any model: fakes only.
- Timeouts of the three decision jobs are unchanged, hand-over included.

## Success demo

Done when, on a stack where the reply runs on one model and the boundary
check, sentiment and department are set to TypeSafe's decision model, a
customer message is answered; the recorded calls show which model served
each job and its use; a reply that breaks a boundary is blocked, and is
blocked when the decision model errors or times out; an agent with more
`never` items than the decision model accepts has its check handed to the
agent's model and recorded so; the comparison command prints the stored
replies on which a candidate model disagrees; and with no new setting every
existing test passes unchanged. The decision model is a fake in tests; the
real service is exercised only when `TYPESAFE_API_KEY` is set.

## Grill log

| # | question | recommendation | answer | tag |
|---|----------|----------------|--------|-----|
| 1 | Mode | (given by the human) | mvp | AGREE |
| 2 | How is the boundary reason kept useful without free text? | The pick of the broken rule; no second call | As recommended; `check_failed` and `check_unparseable` stay | AGREE |
| 3 | Jobs set per agent too, or per server only? | Per server only | Per server only, in the environment | AGREE |
| 4 | Do the two writing jobs get a setting? | Yes, same mechanism; refused at startup on a decision model | As recommended; the guide states the risk | AGREE |
| 5 | Add a pre-check that skips the model for simple messages? | No, later, with data | No | AGREE |
| 6 | What happens when the decision model cannot answer a call? | That call goes to the agent's model, for every cause including errors and timeouts | Hand over only what it cannot take (too many options, an image, unsure). An error or timeout fails as today, with no hand-over | DIVERGE: the brief already decides "failing the same way" and "timeouts unchanged"; a retry after an error would reopen that and add latency |
