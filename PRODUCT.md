---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: human                   # human | agent (set by comb:auto)
---

# Docs an agent can follow

## What / why

Parley's docs grew one run at a time. The README is 900 lines of setup,
reference and history mixed together, and cites tickets and spec
sections that no longer exist. An AI agent asked to "add parley to this
project" has no entry point, no single list of what must be configured,
and no ordered path from an empty stack to a working conversation. This
run makes the docs say what is implemented, and adds an entry point and
guides an agent can follow to wire parley into another application.

In scope (existing repo):

- **An entry point for agents: `llms.txt`.** At the repo root, in the
  llms.txt convention: what parley is in a few lines, then links to each
  guide with one line on when to read it.
- **Integration guides an agent can execute.** Ordered steps with exact
  requests and expected responses: run the stack; configure it (every
  environment variable, which are required, what each does); create a
  tenant, an agent, a channel and tools; send and receive messages on
  webchat and on WhatsApp through Evolution; hand a conversation to a
  human and build an operator screen on the API (lines, claim, reply,
  resume); categories; memory; boundaries; failure texts; errors,
  paging and polling; health and readiness. Each guide says how to
  check that its step worked.
- **A running instance serves them.** `GET /llms.txt` and the guides it
  links to, without authentication, beside the existing `/openapi.json`.
  An agent given only a base URL can start from there.
- **A short `AGENTS.md` for contributors.** For an agent changing
  parley's own code: how to run the checks, where things live, the
  rules that are easy to break.
- **The existing docs are tightened.** README becomes a short overview,
  a quick start and links. ARCHITECTURE keeps how the system works and
  loses repetition and history asides. Statements that no longer match
  the code are corrected or removed.
- **Docs cannot drift silently.** A test fails when the docs name an
  endpoint, an environment variable or an agent field the code does not
  have.

Decided in the grill:

- Audience: integrators first; contributors get one short file.
- Delivery: in the repo and served by the API.
- History is kept: `docs/FOLLOW_UPS.md` keeps its per-run sections and
  done markers, reorganized for reading; `PRD.md` is committed as a
  record. Only README and ARCHITECTURE are cut down.
- Proof: the drift test, plus one run by a fresh agent that sees only
  `llms.txt` and its links, on a throwaway stack with a stub in place
  of the model. Where it stumbles, the docs are fixed.
- Docs stay in English.

## Primary user story

As a developer, I tell my coding agent "integrate parley into this app"
and give it the repo or a base URL; it reads `llms.txt`, follows the
guides, and ends with a configured tenant, a working channel and an
operator screen talking to the API, without me explaining parley to it.

## Out of scope

- Client libraries or SDKs; a generated API reference beyond what
  `/openapi.json` already gives
- A docs website, search, versioned docs, translations
- Tutorials for building a whole product on parley; sample apps
- New features or behaviour changes in the API (except serving the docs)
- Rewriting `docs/FOLLOW_UPS.md` items or deleting history
- An MCP server for parley

## Success demo

Done when I can give a fresh agent nothing but `llms.txt` (from the repo
or from `GET /llms.txt` on a running instance) and watch it bring up a
stack, create a tenant, agent, channel and tool, exchange a message,
hand off to a human, answer as an operator and resume — reading only
what `llms.txt` links to — and the suite fails when a doc names an
endpoint, environment variable or agent field that does not exist.
