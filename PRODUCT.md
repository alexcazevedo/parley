---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: human                   # human | agent (set by comb:auto)
---

# Ops and hygiene (PRD epic E7)

## What / why

The last epic of the hardening backlog. The image runs as root and
installs its package manager from an unpinned script; every API replica
runs the migrations at start; the database and Redis are published on
the host with default credentials; a worker log line does not say which
conversation it is about; a caller can write anything into the request
id; the texts a customer is sent on failure are fixed in Portuguese; and
the project has two names. None of it is a feature. All of it is what an
operator trips over.

In scope (existing repo):

- **The image runs as a non-root user**, with uv copied from a pinned
  image instead of piped from a script.
- **Migrations run once.** A one-shot `migrate` service runs them; the
  API and the worker start after it succeeded. The API no longer
  migrates at start.
- **The database and Redis are not published by default.** A separate
  `docker-compose.dev.yml` publishes them for work from the host;
  `make db` uses it.
- **One logging library.** The two routers still on stdlib `logging`
  move to structlog.
- **Worker logs carry context.** Every line logged while a turn runs has
  `tenant_id` and `conversation_id`; lines from the agent run also have
  `run_id`.
- **The request id is checked.** A well-formed `X-Request-ID` is kept;
  anything else is replaced by a generated one.
- **Customer-facing failure texts are per agent.** The technical-error
  text and the unsupported-media text can be set on the agent; the
  current Portuguese texts stay as defaults. The placeholders the model
  sees for an image or an audio become settings.
- **One name: parley.** Package name, API title, README and docs. Stale
  "ticket NN" comments in the code are reworded or removed.

Decided in the grill:

- E7.6 (per-tenant LLM keys, cost accounting, metrics) is left out and
  recorded in `docs/FOLLOW_UPS.md` (user).
- Name: parley. Redis key prefixes (`harness:`), the database name and
  the database user stay: renaming them needs a data migration (user).
- Ports: separate dev file, not loopback binding (user).
- `STT_LANGUAGE` stays a deployment setting: it is already configurable,
  and transcription runs before an agent is known.
- E7.5 and the queue wording in E7.3 need no work: earlier runs already
  removed the inline imports, the duplicated connection code and the
  BRPOPLPUSH wording.
- The README, architecture doc and follow-ups describe the changes.

## Primary user story

As the person operating parley, I start the stack and it migrates once,
runs unprivileged and exposes only the API; when a turn fails I find
every log line of that conversation by its id; and I can tell each agent
what to say, in its customers' language, when something goes wrong.

## Out of scope

- Per-tenant LLM keys, token and cost accounting, metrics (E7.6)
- Renaming Redis keys, the database, the database user or env variables
- Per-agent or per-tenant transcription language
- Translating the default texts; a language setting that switches them
- Building or pushing the image in CI; Kubernetes manifests
- Secrets management, TLS, credentials rotation
- The rest of `docs/FOLLOW_UPS.md`

## Success demo

Done when I can build the image and see it run as a non-root user,
start the stack with two API replicas and see the migrations run once
and both replicas answer `/ready`, find no database or Redis port on the
host unless I use the dev file, read a worker log line of a turn and see
its `tenant_id` and `conversation_id`, send a request with a malformed
`X-Request-ID` and get a generated one back, and set an agent's error
text and see a customer receive it — and the suite proves the logging,
request-id and text behaviour, and still passes.
