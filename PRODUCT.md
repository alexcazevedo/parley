---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: agent                   # human | agent (set by comb:auto)
---

# Knowledge base (milestone 3 of 5)

## What / why

A tenant's own material (opening hours, exchange and return policy, payment
and delivery rules, question-and-answer pairs, operation documents) can
today only go in the agent's `instructions`, which fills the context on
every turn and does not scale. This milestone gives each tenant a knowledge
base that the agent searches on demand. The content never sits in the
prompt. First tenant: vendezap.

## Primary user story

As an integrator with a tenant key, I store entries, question-and-answer
pairs and documents, switch `knowledge_enabled` on for an agent, and the
agent answers customers from that material, or hands off or says it does
not know. It never invents an answer.

## In scope

**Items**, tenant-scoped, through the HTTP API with the tenant key: entries
(title and text), question-and-answer pairs, and documents (uploaded PDF,
plain text or Markdown, turned into text with the existing PDF extraction
and its limits). Create, read, paged list, update, delete. Every item has a
status: processing, ready, or failed with a reason. Delete removes every
passage of the item.

**Documents.** Parley keeps the extracted text only: no stored file, no
download. The title can be changed; the content cannot. To replace a
document, upload the new one, wait for ready, delete the old one. Entries
and pairs are fully editable; an edit is split again in the worker.

**Limits.** Set once by the server owner in the environment, the same for
every tenant, counted per tenant: number of items, size of one entry or
answer, the existing upload and PDF limits, total stored text. Going over
is refused with an error naming the limit.

**Search.** Content is split into passages in the worker, never in the
request. Keyword search (Postgres full-text, Portuguese) always works, on
plain Postgres, with no extra service. Semantic search (pgvector) is added
when an OpenAI-compatible embeddings endpoint is set in the environment;
both results are merged. A re-index command recovers from a change of
embeddings model. A tenant never reads another tenant's passages.

**The agent.** A built-in tool, `search_knowledge(query)`, switched per
agent by `knowledge_enabled` (default off: existing agents do not change).
It returns the few best passages with their source titles, under a fixed
size budget. The system prompt gains only a short, capped list of titles.
Retrieved text is data, not instructions; the boundary check still applies
to the reply. The agent run records which passages were returned.

**Try a search.** A route: query in, passages and scores out, as the agent
would get them.

**Manifest.** An optional `knowledge` section with entries (identified by
title, unique per tenant) and pairs (identified by the question), applied
like departments. Documents stay API-only: export leaves them out with a
warning naming each; apply never touches them. Older files stay valid.

**Tests.** Ingestion, both search modes, the tool inside a turn, limits,
deletion, tenant isolation. No real model or embeddings call.

**Docs.** A knowledge-base guide in `docs/guides`; the setup, manifest and
agent-behaviour guides; `llms.txt`; `docs/integrations/vendezap.md`.

## Out of scope

Admin screen (vendezap builds its own on this API), crawling websites,
images and audio as knowledge, Word and spreadsheet files, OCR of scanned
PDFs, per-category or per-channel knowledge bases, per-tenant limit
overrides, replacing a document's file in place, downloading the original
file, answers without the agent.

## Constraints

- Follow the patterns already in the code; add no new ones.
- Existing endpoints and agents behave as before.
- A server without the embeddings setting, on plain `postgres:16`, still
  migrates, starts and searches by keyword.

## Success demo

Done when, with a tenant key, I can add an entry, a question-and-answer
pair and a PDF, see the PDF become ready, switch `knowledge_enabled` on for
an agent, send a customer message on a channel that only the PDF can
answer, and see in the agent run that `search_knowledge` was called and the
reply uses the passage; and the same question to an agent with the switch
off makes no such call.

## Grill log

| # | question | recommendation | answer | tag |
|---|----------|----------------|--------|-----|
| 1 | Mode | (given by the human) | mvp | AGREE |
| 2 | How does the knowledge base enter the manifest? | Optional `knowledge` section for entries and pairs; documents API-only, named in an export warning | As recommended. Entries keyed by title, pairs by question; older files stay valid | AGREE |
| 3 | What does full CRUD mean for a document; is the original file kept? | Text only, no download; title editable, content not; replace by upload then delete; entries and pairs fully editable | As recommended | AGREE |
| 4 | Who sets the per-tenant limits? | Server owner, in the environment, same for every tenant, counted per tenant; no override | As recommended | AGREE |
