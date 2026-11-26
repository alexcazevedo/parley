# REPORT: Business tools over MCP (milestone 4)

- **Vision:** implement `docs/roadmap/1-business-tools-mcp.md`: an app runs an MCP server, and the parley agent calls its tools for the customer who is writing.
- **Mode:** mvp. **Owner:** agent.
- **Outcome:** done. All 7 tickets finished; `make check` is green (1310 passed, 1 skipped). Committed and pushed on branch `feat/mcp-tools`.

## Grill

AGREE 6, DIVERGE 0, ESCALATE 0. No DIVERGE or ESCALATE rows.

The five open items of the brief were decided as: the official MCP SDK, kept in one client module; the signature is a documented follow-up; tools attach per server, with an optional allow-list on the server; a server that asks for OAuth registers and shows status `error` with the reason; the stub model calls a tool on `/tool <name> <JSON arguments>`.

Plan verdict from the owner: APPROVE, no objections.

## Tickets

| # | ticket | model | status | attempts |
|---|---|---|---|---|
| 01 | Customer context and call id on every tool call | opus | done | 2 of 3 |
| 02 | MCP servers: registration, discovery, re-sync | opus | done | 1 of 3 |
| 03 | Agent field `mcp_servers`, executor, try route, stub command | opus | done | 1 of 3 |
| 04 | Manifest `mcp_servers` section | sonnet | done | 1 of 3 |
| 05 | Example project MCP server | sonnet | done | 1 of 3 |
| 06 | Success demo (test and live script) | sonnet | done | 2 of 3 |
| 07 | Docs | sonnet | done | 1 of 3 |

No blocked tickets.

## The success demo

- **Test, scripted model, natural sentences** (`tests/test_mcp_demo.py`): "Vocês têm cartolina branca?" makes the agent call `shop__search_product` and reply with both products; "Sim, a primeira" makes it call `shop__add_to_quote`; the item is in that customer's quote and in no other; the same call id again adds nothing; a second customer gets their own quote; both `tool_executions` rows hold the customer context.
- **Live, stub stack with the example project, no model key:** the script printed `business tools demo: ok`. On the stub the customer sends `/tool ...` commands in place of the sentences, because the stub does not reason.

## What did not go as planned

- **Ticket 01, attempt 1:** an existing retry test calls the HTTP executor directly and broke on the new header argument; the argument became optional.
- **Ticket 06, first live run:** registering a server with a token returned 500. `.env` on this machine has a placeholder `SECRETS_KEY`, and encrypting the token needs a real key. `.env` was not edited; the new `docker-compose.demo.yml` sets `DEMO_MODE` and takes a key made for the run.
- **Ticket 04** also edited `app/api/manifest.py` (the sync after the apply commits), which I added to its files at dispatch.

## How to run the result

```
export DEMO_SECRETS_KEY=$(uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
docker compose -f docker-compose.yml -f docker-compose.stub.yml -f docker-compose.example.yml -f docker-compose.demo.yml up -d --build --wait
uv run python scripts/demo_business_tools.py --base-url http://localhost:8001 --inbox-url http://localhost:9200 --admin-key <the ADMIN_API_KEY of .env> --tools-token demo-tools-token
```

It prints `business tools demo: ok`. In tests: `uv run pytest -q tests/test_mcp_demo.py`.

## Final review: diff summary

68 files changed, 5026 insertions, 265 deletions, against `392365e`.

**New behaviour**
- Routes, tenant key: `POST /mcp-servers`, `GET /mcp-servers`, `GET /mcp-servers/{server_id}`, `PATCH /mcp-servers/{server_id}`, `DELETE /mcp-servers/{server_id}`, `POST /mcp-servers/{server_id}/sync`, `POST /mcp-servers/{server_id}/call` (try one tool).
- Agent field `mcp_servers` (server names, default empty). The model sees each tool as `<server>__<tool>`.
- Every tool call, MCP and HTTP, carries the conversation id, channel id, the app's own user id, the contact name when known, and a call id. MCP: in `_meta` under `parley/...`. HTTP tools: as `X-Parley-...` headers. Each call's row in `tool_executions` stores them (`call_id`, `context`).
- Discovery at registration, on request and every `MCP_SYNC_INTERVAL_SECONDS` (300) in the worker. A failing server keeps its last tools and shows `error` with a reason.
- A read-only tool may be repeated; any other is not. An error result reaches the model as a refusal it can explain.
- Manifest section `mcp_servers`; an agent naming an unknown server is refused before any write.
- Stub model: `/tool <name> <JSON arguments>`.
- Example project: MCP server at `/mcp` with `search_product` and `add_to_quote`, `GET /quotes`.

**Where the code is**
- New: `app/mcp/` (client, sync), `app/tools/mcp.py`, `app/tools/context.py`, `app/api/mcp_servers.py`, `app/repositories/mcp.py`, `app/models/mcp_server.py`, `app/models/mcp_tool.py`, `examples/project_inbox/tools.py`, `scripts/demo_business_tools.py`, `docker-compose.demo.yml`.
- Changed: `app/tools/` (base, ledger, registry, http), `app/agents/runtime.py`, `app/api/agents.py`, `app/models/agent.py`, `app/models/tool_execution.py`, `app/manifest/`, `app/api/manifest.py`, `app/workers/main.py`, `app/config.py`, `scripts/mock_llm_server.py`, `examples/project_inbox/app.py`, `docker-compose.example.yml`.
- Migrations `0025` (call id and context on tool executions), `0026` (MCP tables), `0027` (`mcp_servers` on agents), `0028` (manifest kind).
- New dependency: `mcp>=2.3.0`. It brings a second HTTP client library (`httpx2`) and a web server stack.
- Docs: new `docs/guides/business-tools.md` (the app-side contract, 14 steps); pointers in setup, manifest, agent-behaviour, project-channel, operations, quickstart; `llms.txt`, `README.md`, `AGENTS.md`, `ARCHITECTURE.md`, `FOLLOW_UPS.md`, the vendezap brief, the roadmap index.
- Tests: 6 new test files, about 60 tests, including tenant isolation on servers, on tool calls and on the context.

**Decisions to check**
1. **Resolved after review: how "the call never ran" is told from "outcome unknown".** `McpError` now carries `sent`, set from the failure's type (address refused, connection never opened, token refused: not sent; a read timeout, a broken connection, a protocol error: unknown). No reason text is matched. This also corrected a case the text matching had wrong: a connection that broke after it opened used to count as "never ran".
2. **Resolved after review: no key in the repository.** `docker-compose.demo.yml` takes `DEMO_SECRETS_KEY` from the shell and refuses to start without it; the guide shows the one line that makes a key for the run. The live demo was run again this way and passed.
3. **Renaming a server detaches it from agents**, because agents list servers by name. Recorded as a follow-up.
4. **A refusal reaches the model with a prefix** added by the SDK: `Error executing tool <name>: <your message>`.
5. **Host names.** The SDK's server refuses (421) a request whose `Host` is not localhost unless the app allows the name. The example does; the guide warns app authors.
6. **Not built, as decided:** the request signature and picking single tools per agent. Both are in `docs/FOLLOW_UPS.md`.

**Not verified**
- No real model was called: only the scripted fake and the stub. Whether a real model picks `search_product` from its description, and carries the product id into `add_to_quote`, is untested.
- No third-party MCP server was tried: only the SDK's own server, in process and in the example.
- OAuth detection was tested against a simulated 401, not a real OAuth server.
- CI was not run.
