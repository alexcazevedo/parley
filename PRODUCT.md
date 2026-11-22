---
mode: production               # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: human                   # human | agent (set by comb:auto)
---

# Categories and waiting lines

## What / why

Conversations are one flat list today. Nobody can see what they are
about without opening them, and a conversation handed to a human waits
in no particular order with nothing to stop two operators from taking
it. This run gives every conversation a subject, chosen by the model
from a list the tenant defined, and gives handed-off conversations a
waiting line per subject that operators pick from.

The harness owns the facts and the race: the category of a conversation,
since when it waits, who claimed it. The app that uses the harness owns
people and policy: operator accounts, who may see which line, priorities,
notifications, the real operator screens.

In scope (existing repo):

- **Categories are defined by the tenant.** One list per tenant, shared
  by all its agents and channels. Each category has a name and a short
  description the model reads. Managed through the API.
- **A conversation has one category or none.** Never several.
- **The model categorizes, and only from the list.** Every conversation
  is categorized from the customer's first messages and checked again on
  later messages, so the category follows a change of subject. The model
  never creates a category; when nothing fits, the conversation stays
  uncategorized.
- **A human can move a conversation, and the human wins.** After a
  category is set through the API, the model no longer changes it for
  that conversation.
- **Conversations can be listed by category**, whatever their state.
- **A waiting line per category.** A conversation enters the line when
  it is handed to a human. The line is ordered by handoff time, oldest
  first. Uncategorized conversations wait in a line of their own.
- **A move keeps the place earned.** A conversation moved to another
  category, by the model or by a human, joins the new line at the
  position of its original handoff time.
- **Picking is exclusive.** An operator claims a conversation with an
  operator id supplied by the app; the harness stores it and does not
  check it. A claimed conversation leaves the line. A second claim is
  refused. A release puts it back at its original position. Resuming the
  AI or closing the conversation clears the claim.
- **The `/operator` demo page shows it.** Lines grouped by category with
  the wait time, a pick button, and a selector to move a conversation.

Decided in the grill:

- A tenant with no categories has the feature off: no classifier call is
  made and handed-off conversations wait in the one uncategorized line.
- Deleting a category makes its conversations uncategorized; they keep
  their wait position.
- The README, architecture doc and `docs/FOLLOW_UPS.md` describe the new
  behaviour and its limits.

## Primary user story

As someone running parley for my customers, I define the subjects my
business deals with once; from then on every conversation is filed under
one of them without my help, and when a customer asks for a person, my
operators see a line per subject and take the customer who has waited
longest, without two of them answering the same one.

## Out of scope

- Operator accounts, roles and who may see which line
- Priorities, SLA timers, notifications, unread counters
- Assigning conversations to operators automatically
- Several categories or tags on one conversation
- Categories per agent; the model proposing new categories
- Reports and counts per category
- Operator screens beyond the demo page (no category admin page)
- PRD epics E5–E7 and the rest of `docs/FOLLOW_UPS.md`

## Success demo

Done when I can define "billing" and "support" for a tenant, send a
billing question and a support question from two contacts and see each
conversation filed under the right category, ask for a person in both
and see each waiting in its own line on `/operator` in the order they
asked, move one to the other line and see it placed by its original wait
time, pick it as one operator and be refused as a second, release it and
see it back in its place — and the test suite proves the same, plus that
a human's choice is not overwritten by the model, that a tenant without
categories makes no classifier call, and that the existing suite still
passes.
