---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: agent                   # human | agent (set by comb:auto)
---

# Business tools over MCP (milestone 4)

The brief is `docs/roadmap/1-business-tools-mcp.md`; the decisions common to
the roadmap are in `docs/roadmap/README.md`. This file is the short form.

## What / why

An agent can call a tenant's HTTP tools today, but the call carries only the
model's arguments: the app cannot know which customer it is for, each tool
is registered by hand, and no document says what an app must implement. This
milestone lets an app expose its functions by running an MCP server. Parley
connects as a client, discovers the tools, and calls them for the customer
who is writing. First tenant: vendezap (search the catalog, add to a quote).

## Primary user story

As an app developer, I run an MCP server with my business functions,
register its URL in parley with my tenant key, attach it to an agent, and
the agent calls my tools with the customer's identity and a call id, so my
writes land on the right customer and are safe to receive twice.

## In scope

**MCP servers.** Registered with the tenant key: name, URL (streamable HTTP
only), a bearer or header token stored encrypted, an optional allow-list of
tool names. Create, read, paged list, update, delete. The URL passes the
tool address checks (`TOOL_ALLOWED_HOSTS`, the SSRF guard) at registration
and before every connection; redirects are not followed.

**Discovery.** Run at registration, on request and on a schedule in the
worker. Parley keeps a copy of each tool, so a turn never waits on
discovery. Tools that disappear are removed. A server that cannot be
reached, refuses the token or asks for OAuth keeps its last known tools and
shows status `error` with the reason.

**Attaching.** An agent lists server names and gets every imported tool of
each, including tools added later. The model sees each tool under the
server's name as a prefix.

**On every tool call**, MCP and HTTP alike, beside the model's arguments and
never inside them: the conversation id, the channel id, the user id the app
gave parley for that customer, the contact's name when known, and a call id
that stays the same when parley repeats the call. MCP: in `_meta`. HTTP
tools: as headers. Existing HTTP tools keep working.

**Repeats and results.** A tool marked read-only may be retried; any other
is called at most once per model request. An error result is a refusal the
model may explain; a transport failure is a failure. Results are data, not
instructions; the boundary check still applies. Every call is recorded in
`tool_executions` with its context.

**Also.** Manifest support for servers and their attachment. A route to try
one tool call. The stub model calls a tenant tool on the command
`/tool <name> <JSON arguments>`. The example project gains an MCP server
with `search_product` and `add_to_quote`. An app-side guide written as the
contract, and a section in the vendezap brief.

## Out of scope

The request signature (a documented follow-up; the token over https is the
minimum). Picking single tools per agent. Parley as an MCP server. The stdio
transport. MCP resources, prompts, sampling, elicitation. OAuth. Importing
OpenAPI. Confirmation before a tool that changes something. Tools returning
files or images. Per-customer permissions on tools. Any change in the
vendezap repository. Any admin screen. Any agent framework.

## Constraints

- Follow the patterns already in the code; the official MCP Python SDK is
  the only new dependency, kept inside one client module.
- No real model or network call in tests: an in-process MCP server.
- Nothing changes for a tenant that registers no server.

## Success demo

Done when, with the example project beside parley and a scripted model, I
register the example's MCP server with one call, attach it to an agent, and
send "Vocês têm cartolina branca?" on an external channel: the agent calls
`search_product` and replies with the two products; I answer "Sim, a
primeira": it calls `add_to_quote`, the example shows the item in that
customer's quote and no other, and the agent confirms; the same add with
the same call id adds nothing; a second customer gets their own quote;
`tool_executions` shows both calls with the customer context; and the same
path runs on the stub stack with no model key.

## Grill log

| # | question | recommendation | answer | tag |
|---|----------|----------------|--------|-----|
| 1 | Mode | (given by the human) | mvp | AGREE |
| 2 | Official MCP SDK or a client of parley's own? | The SDK: follows the protocol, takes a caller's HTTP client, tests in process | The SDK, kept inside one client module; every connection goes through the address checks with redirects off | AGREE |
| 3 | Signature now or later? | Later; context and call id now, authenticated by the token over https | Documented follow-up; the guide says no signature is sent yet | AGREE |
| 4 | How are tools attached to an agent? | Per server, with an optional allow-list on the server; no per-agent picking | As recommended | AGREE |
| 5 | A server that requires OAuth? | Registration succeeds; discovery fails; status `error` with the reason | As recommended: one failure path, also for a server that switches to OAuth later | AGREE |
| 6 | How does the stub model call a tenant tool? | The command `/tool <name> <JSON arguments>` | As recommended; the natural sentences are covered by the scripted-model test | AGREE |
