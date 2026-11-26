---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: human                   # human | agent (set by comb:auto)
---

# One-step setup (plug and play, milestone 2 of 5)

## What / why

Setting up a business in parley takes about ten ordered API calls: agent,
tools, attaching tools, departments, channels. Each project repeats them by
hand or in a script, nothing records what the setup should be, and changing
it later means finding ids and patching one item at a time.

This milestone lets a project describe its business in one file (the
manifest) and apply it with one call, as often as it wants. The file can
live in the project's repository and is the record of the setup.

## Primary user story

As a developer (or an AI agent) integrating a business, I write one file
with the agent's persona, the departments, the tools and the channels, apply
it with my tenant key, and the agent works. To change anything I edit the
file and apply it again.

## In scope

**The manifest.** One document, accepted as JSON or YAML, with a version
number. It holds: agents (every field the agents API accepts, including the
persona), departments (name and the description the agent routes by), tools
(every field the tools API accepts), which tools each agent uses, and
channels of every type (web chat, WhatsApp through Evolution, external),
each bound to an agent. Agents and channels carry a key chosen by the
author, because their names are not unique; departments and tools are
identified by name.

**Apply.** One call with the tenant key. The whole file is validated first,
with every problem reported at once; then all changes are made together or
none are. Applying the same file twice changes nothing. The answer lists
what was created, updated, deleted and left alone. A secret that parley
generates (an external channel's signing secret) is returned once, on the
apply that creates the channel.

**Dry run.** The same call in a mode that changes nothing and returns the
same list.

**Removed items.** Parley remembers which items the manifest manages. An
item removed from the file is deleted on the next apply; the dry run shows
it first. Items created by hand through the API are never touched. An
agent or channel that has any conversation, open or closed, is refused
(deleting it would erase that history), as is a department used by an open
conversation; the message says why and nothing is changed.

**Secrets.** The file never holds a credential. It names secrets; their
values are set separately through a write-only endpoint, stored encrypted,
replaceable, never readable. Apply fails with the list of missing names.

**Existing tenants.** Export writes the manifest of a tenant as it is now
(secrets as names to be set, never values). Applying a file adopts an
existing item with the same name or key instead of duplicating it; from
then on the manifest manages it.

**Tenant.** Created first by the server owner with the admin key, as
today. The manifest applies to that tenant only.

**Docs.** A guide, `docs/guides/manifest.md`, linked from `llms.txt`,
passing the drift and replay tests, with one complete example manifest for
a store, and shorter ones for a clinic and a law firm. The quickstart
points to it as the fast path.

## Out of scope

Later milestones; this one must leave room for each:
- **Knowledge base**: the manifest's version number and layout must allow a
  knowledge section to be added without breaking older files.
- **Business functions as tools** (importing an OpenAPI spec, confirmation
  before risky actions): tools in the manifest are today's HTTP tools.
- **Admin UI**: export and apply are the one document a UI will read and
  write; nothing here is possible only through the file.

Never: creating tenants from the manifest, a command-line tool, conversations
or contacts or memories in the manifest, history of past manifests or
rollback, applying to several tenants at once, billing, hosted SaaS.

## Constraints

- Existing endpoints keep working; hand-made setups are unaffected until a
  manifest adopts them.
- Everything is tenant-scoped.
- The manifest goes through the same validation as the existing endpoints
  (bounds, tool name rules, address checks): nothing is possible through
  the file that the API refuses.

## Success demo

Done when a fresh agent, given only `llms.txt` and one paragraph describing
a made-up clinic, writes a manifest, sets its one secret, runs the dry run,
applies it to an empty tenant on a stack running the stub model, and a
customer message is answered; then removes a department and changes the
persona in the file, applies again, and the dry run of a third apply
reports nothing to do; with no other setup call.

## Grill log

| # | Question | Answer |
|---|---|---|
| 1 | Mode | mvp |
| 2 | Items removed from the file | Delete only what the manifest created; refuse if in use |
| 3 | Secrets | Named in the file, values set separately, write-only |
| 4 | Tenants already set up by hand | Export, and adopt by name or key |
| 5 | Creating the tenant | Server owner first; the manifest applies with the tenant key |
