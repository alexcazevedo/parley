# Parley

Parley is a headless, multi-tenant service that runs AI conversations over HTTP. Your application talks to it through a REST API; it is not a library and it has no product UI of its own (two demo pages aside).

**What parley owns:** the operational side of a conversation. It takes in messages, waits for a customer to finish typing, keeps the conversation's state, runs the model with the agent's instructions and tools, remembers facts about a contact, hands a conversation to a human, retries a failed turn and logs what happened.

**What your application owns:** the business logic, as HTTP tools parley calls; the channel (your own web chat, or WhatsApp through Evolution API); operator accounts and the operator screen; notifications; the customer's language and the texts parley sends.

## What it does today

- Tenants with their own API key; every query is scoped to the tenant.
- Agents with instructions, a model, tools, long-term memory and a rolling summary.
- Business tools: an MCP server you run, discovered by parley and called for the customer who is writing, or HTTP endpoints you describe with a JSON Schema; both guarded against calls to private addresses.
- Channels: web chat over REST, and WhatsApp through Evolution API, with images and voice notes (transcribed).
- A debounce window turns a burst of messages into one agent turn; a message that arrives mid-reply supersedes the turn.
- Human handoff: by the agent's tool, by a frustration score, or by an operator. A handoff carries a short AI-written brief.
- Waiting lines per category, claim and release, so an app can build an operator screen on the API.
- Per-agent boundaries with a reply check, and an optional deployment-wide floor.
- Optional humanized replies: short bubbles with typing pauses.
- At-least-once delivery with recovery from a dead worker, turn retries and a dead-letter list.
- `GET /health`, `GET /ready`, JSON logs with request and conversation ids.

Not included: client libraries, streaming (clients poll), operator accounts, a model other than OpenAI-compatible chat completions.

## Quick start

You need Docker with Compose, `curl` and `jq`.

```bash
cp .env.example .env
# edit .env: set ADMIN_API_KEY, SECRETS_KEY (a Fernet key) and LLM_API_KEY
docker compose up -d --build
curl -s http://localhost:8001/ready
```

Expected: `{"status":"ready","checks":{"database":"ok","redis":"ok"}}`. The API and the worker refuse to start while `ADMIN_API_KEY` or `SECRETS_KEY` is a placeholder, unless `DEMO_MODE=true`.

No model key? Run the stack with a stub model that answers `Stub reply to: <your message>` and hands off when the message asks for a person:

```bash
docker compose -f docker-compose.yml -f docker-compose.stub.yml up -d --build
```

Two demo pages are served by the API:

- `http://localhost:8001/chat`: a chat page. It needs `DEMO_MODE=true` and keeps the tenant key in the browser, so never use it for real customers.
- `http://localhost:8001/operator`: an operator inbox. Paste a tenant key.

The rest of the path (tenant, agent, tool, channel, first message, handoff) is in [docs/guides/quickstart.md](docs/guides/quickstart.md) and the guides below.

## Integrating parley

An agent asked to add parley to a project should start from [llms.txt](llms.txt). A running instance serves the same file at `GET /llms.txt`, and the guides it links to under `/docs/guides/`, without authentication. `GET /openapi.json` is the machine-readable API.

Follow the guides in order; each one ends with a check that its step worked.

- [Quickstart](docs/guides/quickstart.md): bring up the stack and confirm it is healthy.
- [Configuration](docs/guides/configuration.md): every environment variable, and which are required.
- [Setup](docs/guides/setup.md): create a tenant, an agent, a channel and tools.
- [Manifest](docs/guides/manifest.md): describe a business in one file and apply it with one call; secrets, dry run, export of a tenant set up by hand, three example files.
- [Messaging](docs/guides/messaging.md): send and receive messages on web chat and WhatsApp; what the customer sees when something fails.
- [Operators](docs/guides/operators.md): hand a conversation to a human; lines, claim, reply, resume, categories; a polling loop for an operator screen.
- [Project channel](docs/guides/project-channel.md): connect a project with its own inbox: forward messages, signed event pushes, the event feed, departments, PDFs.
- [Business tools](docs/guides/business-tools.md): give the agent tools that act on the business: an MCP server you run, discovery, the customer context and call id, trying a call, the allow-list.
- [Agent behaviour](docs/guides/agent-behaviour.md): instructions, memory, boundaries, sentiment escalation, failure texts, context window.
- [API conventions](docs/guides/api-conventions.md): authentication, errors, paging, polling, limits.
- [Operations](docs/guides/operations.md): replicas, the image user, upgrading, retries, dead letters, shutdown.

## Development

Python 3.12, FastAPI, PostgreSQL (SQLAlchemy 2 and Alembic), Redis, Docker Compose. See [AGENTS.md](AGENTS.md) for how to work in the repository.

```bash
make check
```

`make check` runs `make lint` (`uv run ruff check .`), `make typecheck` (`uv run mypy`) and `make test` (starts Postgres when nothing listens on its port, then `uv run pytest -q`), in that order. `make help` lists every target. The same three run in CI on every push.

## More

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): how the system works inside.
- [docs/FOLLOW_UPS.md](docs/FOLLOW_UPS.md): known limits and open items.
- [LICENSE.md](LICENSE.md): the licence.
