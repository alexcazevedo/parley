# REPORT: Knowledge base (milestone 3 of 5)

- **Vision:** a knowledge base per tenant that the agent searches on demand, so tenant knowledge never sits in the prompt.
- **Mode:** mvp. **Owner:** agent.
- **Outcome:** done. All 7 tickets passed on the first attempt; `make check` is green (1246 passed, 1 skipped). Nothing is committed: the work is in the working tree on branch `fix/track-app-media`, waiting for your review.

## Grill

AGREE 4, DIVERGE 0, ESCALATE 0. No DIVERGE or ESCALATE rows.

Plan verdict from the owner: APPROVE. It raised one point that did not block: unique titles would have refused a replacement document uploaded under the same name. Fixed before the run: titles are unique for entries and pairs only.

Does the manifest fit? Partly, as you asked to be told. Entries and question-and-answer pairs are in an optional `knowledge` section. Documents are not: the manifest is one text file of at most 1 MiB and cannot carry a PDF. Export names each document in a warning; apply never touches them.

## Tickets

| # | ticket | model | status | attempts |
|---|---|---|---|---|
| 01 | Entries, pairs, passages, limits, keyword search, try-a-search route | opus | done | 1 of 3 |
| 02 | Documents (PDF, text, Markdown) | sonnet | done | 1 of 3 |
| 03 | `knowledge_enabled` and the `search_knowledge` tool | opus | done | 1 of 3 |
| 04 | Semantic search, optional pgvector, re-index command | opus | done | 1 of 3 |
| 05 | Manifest `knowledge` section | sonnet | done | 1 of 3 |
| 06 | Success demo (test and script) | sonnet | done, with one check failing as written (below) | 1 of 3 |
| 07 | Docs | sonnet | done | 1 of 3 |

No blocked tickets.

## What did not go as planned

- **Ticket 06, check 6 fails as written on this machine.** `.env` holds the placeholder `ADMIN_API_KEY`, so the API refuses to start in the compose stack. I did not edit `.env`. I ran the same demo with a throwaway override that sets `DEMO_MODE=true`, under its own compose project (`parley-kbdemo`, own volume, removed afterwards). Result: script exit 0; the agent with the switch on answered from the PDF and its run has a `search_knowledge` execution with status `ok`; the agent with the switch off has none.
- **The script cannot show the tool call itself.** No endpoint lists agent runs or tool calls, so the script infers the search from the stub's reply; the test and my database query read the `tool_executions` row. "See in the agent run" today means reading that table.
- **Ticket 05 touched files outside its list:** `app/models/manifest_item.py` and migration `0024`. A check constraint on manifest item kinds refused the new kinds. Ticket 07 also edited `tests/test_llms_served.py` (the fixed order of guides).

## Things I changed outside the repository

- Installed the `postgresql-16-pgvector` package inside the running `parley-test-pg` container, so the semantic tests run instead of skipping. No restart, no data touched. A recreated container from plain `postgres:16` will skip them again; `make db` now starts the pgvector image.

## How to run the result

```
docker compose -f docker-compose.yml -f docker-compose.stub.yml up -d --build --wait
uv run python scripts/demo_knowledge.py --base-url http://localhost:8001 --admin-key <your admin key>
```

It prints `knowledge demo: ok`. The stack needs a real `ADMIN_API_KEY` in `.env` (or `DEMO_MODE=true`). In tests: `uv run pytest -q tests/test_knowledge_demo.py`.

## Final review: diff summary

56 files changed, 4496 insertions, 144 deletions, against `ca5c73e`.

**New behaviour**
- Routes, tenant key: `POST /knowledge/entries`, `POST /knowledge/qa`, `POST /knowledge/documents`, `GET /knowledge`, `GET /knowledge/{item_id}`, `PATCH /knowledge/{item_id}`, `DELETE /knowledge/{item_id}`, `POST /knowledge/search`.
- Agent field `knowledge_enabled` (default off) and built-in tool `search_knowledge`. With the switch off, the prompt and the tool list are unchanged.
- Worker loop that splits items into passages and extracts document text; a document goes `processing`, then `ready` or `failed` with a reason.
- Keyword search always (Portuguese, accents ignored, so `horario` finds `horário`). Semantic search when `EMBEDDINGS_BASE_URL` is set and the database has pgvector; both rankings are merged. Any failure on the semantic path falls back to keyword.
- `uv run python -m app.knowledge.reindex [--all]` after a change of embeddings model.
- Manifest `knowledge` section; export warning per document.

**Where the code is**
- `app/knowledge/` (text, ingest, search, embeddings, reindex), `app/api/knowledge.py`, `app/repositories/knowledge.py`, `app/models/knowledge_item.py`, `app/models/knowledge_passage.py`.
- Changed: `app/tools/builtin.py`, `app/agents/context.py`, `app/agents/runtime.py`, `app/api/agents.py`, `app/models/agent.py`, `app/manifest/` (schema, apply, export), `app/models/manifest_item.py`, `app/workers/main.py`, `app/media/storage.py`, `app/config.py`, `app/main.py`, `scripts/mock_llm_server.py`.
- Migrations `0021` (knowledge tables), `0022` (`knowledge_enabled`), `0023` (embeddings table, only where pgvector is available), `0024` (manifest item kinds).
- Settings: `KNOWLEDGE_MAX_ITEMS` (500), `KNOWLEDGE_MAX_ITEM_CHARS` (20000), `KNOWLEDGE_MAX_TOTAL_CHARS` (2000000), `KNOWLEDGE_INGEST_INTERVAL_SECONDS` (2.0), `EMBEDDINGS_BASE_URL`, `EMBEDDINGS_API_KEY`, `EMBEDDINGS_MODEL`.
- Infrastructure: `docker-compose.yml` and `.github/workflows/ci.yml` now use `pgvector/pgvector:pg16`; the stub stack serves embeddings too.
- Docs: new `docs/guides/knowledge.md`; setup, agent-behaviour, operations, quickstart, configuration, manifest, `ARCHITECTURE.md`, `llms.txt`, `AGENTS.md`, `docs/integrations/vendezap.md`.
- Tests: 6 new test files, about 40 tests, including tenant isolation on the keyword path, the semantic path and the tool.

**Decisions to check, made at plan time or found in the code**
1. A document is cut at the existing `PDF_MAX_CHARS` (20000 characters) and `PDF_MAX_PAGES` (30), and flagged `truncated`. You asked to reuse those limits; for a knowledge document 20000 characters is small (roughly 8 pages). Raising `PDF_MAX_CHARS` also raises it for customer PDFs in a conversation.
2. The compose default image changes to `pgvector/pgvector:pg16`. It is the same Postgres 16; an existing volume should open as it is, but I did not test an upgrade of an existing volume.
3. Semantic results below cosine similarity 0.25 are dropped (`SEMANTIC_MIN_SIMILARITY`, a constant). The right value depends on the embeddings model; it was tested only against fake embeddings.
4. An embeddings failure leaves an item `ready` on keyword search alone; the re-index command fills the gap later.
5. The system prompt lists at most 30 titles of ready items, each cut to 80 characters.
6. Vectors have no approximate index: each semantic query scans the tenant's vectors. Fine at the default limits; not measured.
7. `ARCHITECTURE.md` is at 928 lines of a 930 limit.

**Not verified**
- No real model and no real embeddings endpoint was called: only the scripted fake and the stub. Whether a real model reliably calls `search_knowledge` and refuses to invent is untested.
- CI was not run; the workflow change is unexercised.
