# AGENTS.md

For an agent changing parley's code. Product docs for integrators are in `docs/guides/`; how the system works inside is in `docs/ARCHITECTURE.md`.

## Commands

- `make check`: `uv run ruff check .`, `uv run mypy`, then `uv run pytest -q`, in that order. Run it before you say a change is done.
- `make test ARGS=tests/test_ready.py`: one file (`make db` runs first).
- `uv run mypy` checks `app/` only. `app.security`, `app.queue` and `app.conversations` are strict (flags are listed in `pyproject.toml`); the rest is checked loosely.
- Tests need Postgres on `localhost:5434`. `make db` starts it, or `docker start parley-test-pg` when that container exists. The database `harness_test` is created when missing (`tests/conftest.py`). Redis is `fakeredis`.
- Migrations: `uv run alembic upgrade head`. The `migrate` compose service runs it once; the API does not.
- Stack: `docker compose up -d --build`. Host access to Postgres and Redis needs `docker-compose.dev.yml`.

## Layout

- `app/api/`: thin routers (parse, repository or service, commit, response). `chat` and `dev` mount only with `DEMO_MODE=true`.
- `app/agents/`: context builder, agent runtime (tool loop), boundaries, categorizer, sentiment.
- `app/channels/`: `ChannelAdapter` and its webchat and Evolution implementations.
- `app/conversations/`: resolver, debounce, lock, retry, `process_turn`, humanizer, rolling summary.
- `app/handoff/`: `HandoffService` and the handoff brief.
- `app/knowledge/`: the knowledge base: text splitting, ingest of items into passages, keyword and semantic search, embeddings, the re-index command.
- `app/llm/`: provider protocol and the OpenAI-compatible provider (plus a scripted fake for tests).
- `app/mcp/`: parley as an MCP client: `client.py` (the only module that imports the `mcp` SDK; `connect` is the one place a connection opens) and `sync.py` (discovery of a server's tools).
- `app/media/`: storage, content sniffing, speech-to-text, media preparation.
- `app/memory/`: long-term memory per contact.
- `app/messages/`: `ingest()`, the one entry for inbound messages.
- `app/models/`: SQLAlchemy models, one file per table.
- `app/observability/`: structlog setup and the request-id middleware.
- `app/queue/`: Redis job queue with per-worker processing lists.
- `app/repositories/`: tenant-scoped data access.
- `app/security/`: API keys, Fernet secrets, rate limits, size limits.
- `app/static/`: the two demo pages, `chat.html` and `operator.html`.
- `app/tools/`: tool registry, built-in, HTTP and MCP tools, call context (`context.py`), SSRF guard, call ledger.
- `app/workers/`: job loop, housekeeping, scheduler.
- `app/config.py` (`Settings`), `app/db.py`, `app/http_client.py`, `app/http_retry.py`, `app/audit.py`.
- `migrations/versions/`: numbered Alembic migrations. `scripts/`: demo scripts and the stub model server.

## Rules that are easy to break

- Every query is tenant-scoped: repositories take `tenant_id` first and filter on it (`app/repositories/base.py`). A raw query needs the same filter.
- Hold no transaction across a model, tool, speech-to-text or HTTP call: call `end_transaction(session)` (`app/db.py`) before slow work.
- Conversation state moves only through `HandoffService` (`app/handoff/service.py`); do not assign `conversation.state` elsewhere.
- Atomic transitions are one conditional `UPDATE ... WHERE` (see `claim` and `release` in `app/repositories/conversations.py`), never read-then-write.
- New files must be `git add`ed: `tests/test_repo_tracked.py` fails on an untracked module under `app/`.
- API data reaches the demo pages' DOM only through `textContent` or `createTextNode`, never `innerHTML`.
- Migrations are numbered (`migrations/versions/00NN_name.py`). Tests build the schema from the models (`Base.metadata.create_all`), so a model must declare every index its migration creates.
- Log with structlog; only `app/observability/logging.py` imports stdlib `logging`.
- Tests never use the network: an autouse guard in `tests/conftest.py` refuses any host but loopback. Script the model with `FakeLLMProvider`, mock tools with `respx`, reach an MCP server only through the `mcp_connect` fixture of `tests/mcp_fakes.py`, and use the `fake_handoff_brief` fixture where a test hands off.
- Docs follow `tests/docs_drift.py`: an endpoint is written in backticks as METHOD, a space and the path, an environment variable as its upper-case name in backticks, and both must exist in the code. Every `AgentCreate` field must appear in backticks in `docs/guides/`. A new shell variable in a guide goes in the non-settings set in that file.
- `README.md` stays at 200 lines or fewer and this file at 120 (`tests/test_docs_shape.py`).
