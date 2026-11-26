# REPORT: Streaming (brief 4)

- **Vision:** implement `docs/roadmap/4-streaming.md`: web chat shows the reply as it is written.
- **Mode:** mvp. **Owner:** agent.
- **Outcome:** stopped: the milestone is not wanted now. Nothing was built. The vision asked for exactly this: "if the answer is no, stop and say so instead of building it."

## Grill

AGREE 2, DIVERGE 0, ESCALATE 0. No DIVERGE or ESCALATE rows.

| # | question | recommendation | answer | tag |
|---|----------|----------------|--------|-----|
| 1 | Mode | (given by the human) | mvp | AGREE |
| 2 | Is this milestone wanted now? | No, not now: no channel or customer needs it | No. Stop before planning and build nothing | AGREE |

The remaining open items of the brief, and the question of which provider path would stream, were not asked: they only matter once the milestone is wanted.

## Why not now

- The only tenant in production, vendezap, uses the `external` channel: its own WhatsApp inbox, which delivers whole messages. Streaming is web chat only.
- Every production agent is on `openai_compatible`, served by the hand-written provider, which does not stream. Streaming for the native providers alone would reach no production agent; teaching the hand-written provider to stream changes the path every live reply uses.
- The reply check is on by default. By the brief's own rule such an agent never shows unchecked text: the customer would see "writing" and then the checked reply.
- Briefs 1 to 3 were deployed the same day and are not yet in use.

## When to reopen

A named web chat customer whose agents either have the reply check off, or for whom "writing" followed by the checked reply is worth the work. Decide then whether streaming covers the native providers only or also the hand-written one.

## Tickets

None. No plan was made and no approval was requested.

## How to run the result

Nothing to run. The record of the decision is in `docs/roadmap/4-streaming.md` and `docs/roadmap/README.md`.

## Final review: diff summary

Three documentation files: this report, a status paragraph at the top of the brief, and the brief's row in the roadmap index. No code, no migration, no setting.
