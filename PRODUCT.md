---
mode: production               # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: human                   # human | agent (set by forge:auto)
---

# Close the two holes the E2 review left open

## What / why

The reviewer pass of the E2 security run (commit `ee85fd2`) found two
holes outside that run's scope. Both are logged in
`docs/FOLLOW_UPS.md` § "From the security run". The repo is being
open-sourced, so they should be closed before people deploy it.
Evidence is code reading only: every item starts with a test that fails
on the current code; an item whose reproduction fails is dropped and
reported, not "fixed" blind.

In scope (existing repo):

- **F1 Channel endpoint SSRF.** A tenant sets the Evolution channel's
  `config.base_url`, and the server POSTs to it (media download, send
  message, mark read, typing) with no address check. The same rules as
  for tool endpoints apply to it: only globally routable addresses,
  checked when a channel is created or updated and again on every
  outbound call, redirects not followed.
- **F2 Media download cap.** The Evolution media download is cut off
  once it passes the media size limit, instead of being read into memory
  whole and rejected afterwards.
- **F3 Tenant-scoped agent loading.** Wherever a turn, the agent
  runtime or the handoff summarizer loads the agent of a conversation,
  the agent must belong to the conversation's tenant. If it does not,
  that turn is refused; another tenant's agent, prompt and tools are
  never used.
- **F4 Audit command.** A read-only command lists rows an older version
  could have left behind: channels and conversations that point at
  another tenant's agent, and channels whose `base_url` the new check
  refuses. It changes nothing.

Decided in the grill:

- **Private Evolution hosts** are allowed through a new operator-only
  setting: a list of trusted channel hosts. It is additive: listed hosts
  skip the address check, every other `base_url` must still be public.
  Default is empty. Tenants cannot change it. It is separate from
  `TOOL_ALLOWED_HOSTS`, so a host trusted for tools is not thereby a
  valid channel target.
- **Existing bad rows fail safely.** A refused turn or outbound call
  logs a clear warning that names the row and never crashes the worker
  or the webhook. No automatic repair or migration of customer data.
- The README says how to list a private Evolution host and how to run
  the audit after upgrading.

## Primary user story

As someone running parley for several tenants, I know that a tenant
cannot use a channel's settings to make my server call internal
addresses or exhaust its memory, and that no conversation ever runs
with another tenant's agent, including on a database that predates
these fixes.

## Out of scope

- SSRF DNS-rebinding pin (stays in `docs/FOLLOW_UPS.md`)
- End-user-scoped webchat token (PRD E2.6) — separate run
- Automatic repair, unlinking or deletion of existing rows
- Other channel adapters' outbound calls beyond what exists today
- Changes to tool SSRF rules or `TOOL_ALLOWED_HOSTS` semantics
- Everything else in `docs/FOLLOW_UPS.md` and PRD epics E3–E7

## Success demo

Done when the test suite proves that an Evolution channel with
`base_url` `http://10.0.0.5:8080` is refused at create, at update and at
call unless its host is on the operator's list, an oversized media
download is cut off at the limit, a conversation linked to another
tenant's agent gets no turn and no summary from that agent, and the
audit command lists exactly the offending rows on a seeded database —
and the existing suite still passes.
