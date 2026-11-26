# Knowledge

Give the agent facts about the business (hours, prices, policies, answers to common questions) and let it answer from them. Start from the variables of [setup](setup.md): `$BASE_URL`, `$TENANT_KEY` and `$AGENT_ID`. Errors, paging and limits are in [api-conventions](api-conventions.md); limits of the knowledge base are in [configuration](configuration.md).

Variables captured here with `jq`: `$ENTRY_ID` (step 2), `$DOCUMENT_ID` (step 4) and `$NEW_DOCUMENT_ID` (step 6).

Every request sends `X-API-Key: $TENANT_KEY`. The knowledge base belongs to the tenant: every agent of the tenant that has `knowledge_enabled` reads the same one.

## 1. What to store

There are three kinds of item. All three are split into passages (up to 700 characters) that the agent searches.

| Kind | You send | Use it for |
|---|---|---|
| `entry` | a `title` and a `text` | one topic: opening hours, a policy, a price list |
| `qa` | a `question` and an `answer` | a question customers ask, with the answer you want given |
| `document` | a PDF, `.txt` or `.md` file | text you already have written |

- Keep one topic in each item. A short, specific title helps: the title is part of every passage and the agent sees the list of titles.
- An entry or a pair is stored as you send it. A document is read by the worker, so it is not searchable right away (step 5).
- Entries and pairs are also kept in the [manifest](manifest.md) (step 8). Documents are not.
- Only text is read. A scanned PDF with no text fails (step 5).

## 2. Add an entry

```
curl -s -X POST $BASE_URL/knowledge/entries \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"title": "Opening hours", "text": "We open Monday to Friday, from 9:00 to 18:00. On Saturdays we open from 9:00 to 13:00. We are closed on Sundays and on public holidays."}'
```

Expected: 201 with the item:

```json
{"id": "...", "kind": "entry", "title": "Opening hours", "text": "We open Monday to Friday, ...",
 "filename": null, "status": "processing", "error": null, "chars": 135, "passages": 0,
 "truncated": false, "created_at": "...", "updated_at": "..."}
```

`title` is 1 to 300 characters and `text` is not empty. A second entry with the same `title` gives 409 `title already exists`. `$ENTRY_ID` is the `id`. To change an item, send `PATCH /knowledge/{item_id}` with a new `title`, a new `text` or both; it goes back to `processing` and the old passages stay searchable until the new ones are ready. `DELETE /knowledge/{item_id}` gives 204 and removes the item and its passages. A missing item, or one of another tenant, gives 404 `knowledge item not found`.

Check: `curl -s $BASE_URL/knowledge/$ENTRY_ID -H "X-API-Key: $TENANT_KEY"` returns 200 with the item.

## 3. Add a question and an answer

```
curl -s -X POST $BASE_URL/knowledge/qa \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"question": "Do you deliver on Sundays?", "answer": "No. Deliveries run from Monday to Saturday."}'
```

Expected: 201 with an item of `kind` `qa`. The question is its `title` and the answer is its `text`. Titles are unique within entries and within pairs, so an entry and a pair may share one.

Check: `curl -s "$BASE_URL/knowledge?kind=qa" -H "X-API-Key: $TENANT_KEY"` lists the pair.

## 4. Add a document

Save a text file. `returns.txt`:

```
cat > returns.txt <<'EOF'
Returns policy

You may return any item within 30 days of delivery. The item must be unused and in its original packaging.

Refunds are paid to the original payment method within 10 business days after we receive the item.
EOF
```

Send it:

```
DOCUMENT_ID=$(curl -s -X POST $BASE_URL/knowledge/documents \
  -H "X-API-Key: $TENANT_KEY" \
  -F "title=Returns policy" -F "file=@returns.txt" | jq -r .id)
```

Expected: 201 with an item of `kind` `document`, `status` `processing`, `filename` `returns.txt` and `text` `null` (the text of a document is never returned). `title` is optional: without it the file name is the title. The form field of the file is `file`.

- The file is a PDF (sniffed from its content), or a `.txt` or `.md` file in UTF-8. Anything else gives 415 `unsupported file: send a PDF, a .txt or a .md file`.
- A file larger than `MEDIA_MAX_BYTES` gives 413 `file exceeds max size`.
- A PDF is read up to its first `PDF_MAX_PAGES` pages, and any file is cut at `PDF_MAX_CHARS` characters. Either cut sets `truncated` to `true` once the document is `ready`. A truncated document is searchable but incomplete: split it or raise the settings.
- Documents may share a title (step 6).

Check: the response has `kind` `document` and `status` `processing`.

## 5. Statuses and failures

A new or edited item is `processing`. The worker takes it within `KNOWLEDGE_INGEST_INTERVAL_SECONDS`, splits it into passages and sets `ready` (`passages` is then above 0), or sets `failed` with the reason in `error`. The worker must be running ([operations](operations.md)). Poll the item until the status is not `processing`:

```
curl -s $BASE_URL/knowledge/$DOCUMENT_ID -H "X-API-Key: $TENANT_KEY"
```

Expected: 200. Once the worker has run, `status` is `ready`, `error` is `null` and `passages` is 1 or more.

`GET /knowledge` lists the items, oldest first, paged as in [api-conventions](api-conventions.md). Filter with `kind` (`entry`, `qa` or `document`) and `status` (`processing`, `ready` or `failed`): `curl -s "$BASE_URL/knowledge?status=failed" -H "X-API-Key: $TENANT_KEY"`.

The reasons a document fails (`error`):

| `error` | What to do |
|---|---|
| `no text found in the file (scanned, empty or protected PDF)` | Upload a PDF that has text, or a text file. |
| `the file is not UTF-8 text` | Save the `.txt` or `.md` file as UTF-8. |
| `KNOWLEDGE_MAX_TOTAL_CHARS reached` | Delete items or ask for a larger limit (step 7). |

Any other error is the message of the problem the worker met, for example an unreadable PDF. A failed document keeps nothing: upload it again. A failed entry or pair goes back to `processing` when you edit it. The uploaded file is deleted once it was read, whatever the outcome; parley keeps only the text.

Check: the document of step 4 is `ready` with at least one passage.

## 6. Replace a document

A document's text cannot be edited: `PATCH /knowledge/{item_id}` with a `text` on a document gives 422 `the text of a document cannot be changed; upload a new document`. Replace it in three moves, so searches never find a gap:

1. Upload the new file under the same title. Documents may share a title.

```
NEW_DOCUMENT_ID=$(curl -s -X POST $BASE_URL/knowledge/documents \
  -H "X-API-Key: $TENANT_KEY" \
  -F "title=Returns policy" -F "file=@returns.txt" | jq -r .id)
```

2. Wait until `GET /knowledge/{item_id}` for the new document says `ready`.
3. Delete the old one:

```
curl -s -X DELETE $BASE_URL/knowledge/$DOCUMENT_ID -H "X-API-Key: $TENANT_KEY" -w '%{http_code}\n'
```

Expected: 204. Between the two uploads both documents answer searches. Step 2 matters: delete the old document before the new one is `ready` and the agent has nothing in the meantime.

Check: `curl -s "$BASE_URL/knowledge?kind=document" -H "X-API-Key: $TENANT_KEY"` lists one document, the new one.

## 7. Limits

The limits are per tenant (defaults in [configuration](configuration.md)):

| Setting | Limits | Where it is refused |
|---|---|---|
| `KNOWLEDGE_MAX_ITEMS` | items of every kind | create of an entry, a pair or a document |
| `KNOWLEDGE_MAX_ITEM_CHARS` | the `text` or `answer` of one item | create and edit of an entry or a pair |
| `KNOWLEDGE_MAX_TOTAL_CHARS` | the text of all items together | create and edit of an entry or a pair; for a document, when the worker reads it (it fails, step 5) |

A request past a limit gives 422, and the message names the setting and its value:

```json
{"error": {"code": "http_error", "message": "knowledge limit exceeded: KNOWLEDGE_MAX_ITEMS is 500", "request_id": "..."}}
```

The text of a document is cut at `PDF_MAX_CHARS` instead of being refused (step 4).

Check: an entry whose text is longer than `KNOWLEDGE_MAX_ITEM_CHARS` gives 422 naming `KNOWLEDGE_MAX_ITEM_CHARS`.

## 8. Try a search

Search the way the agent does, before you turn it on. It needs items that are `ready`.

```
curl -s -X POST $BASE_URL/knowledge/search \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"query": "how many days do I have to return an item?"}'
```

Expected: 200 with `mode` and `results`. `query` is 1 to 500 characters; `limit` is optional, 1 to 20 (the agent always uses the default, 4).

```json
{"mode": "keyword",
 "results": [{"passage_id": "...", "item_id": "...", "kind": "document", "title": "Returns policy",
              "text": "Returns policy\nYou may return any item within 30 days of delivery. ...",
              "score": 0.2, "keyword_rank": 1, "semantic_rank": null}]}
```

- Each result is a passage. `text` starts with the item's title, then the passage. Results are cut so that their texts total at most 3000 characters.
- `mode` is `keyword` when only the words are compared, `hybrid` when the search also compared meaning (below).
- In `keyword` mode a passage matches when it has any word of the query, with accents and case ignored and word endings stemmed as in Portuguese (`ts_rank_cd` over a `portuguese` text search; stop words count for nothing). `score` is that rank: higher is better, and it is only meaningful next to the other results of the same query. A query with only stop words finds nothing. `keyword_rank` is the position in that ranking, and `semantic_rank` is `null`.
- Read the scores to judge a result. A result far below the first is a weak match. No result, `"results": []`, means no passage shares a word with the query.
- Search the words customers use, not only yours. If a question finds nothing, add the customer's words to the item or add a pair with that question.

Check: the first result has the title of the document of step 4.

### Keyword and semantic search

Semantic search is off until `EMBEDDINGS_BASE_URL` is set ([configuration](configuration.md)). Then the worker stores a vector for each passage, a search also ranks passages by meaning (cosine similarity of at least 0.25), and the two rankings are merged by reciprocal rank fusion. `mode` is `hybrid`, `score` is the fused score (small numbers, at most about 0.03), and each result has `keyword_rank`, `semantic_rank` or both. A synonym that shares no word with the query is found this way.

- It needs the Postgres `vector` extension. The compose image `pgvector/pgvector:pg16` has it. On plain `postgres:16` the table cannot be made, and search stays `keyword`.
- Any failure of the embeddings endpoint gives the keyword result (`mode` `keyword`): search never fails because of it.
- After you change `EMBEDDINGS_MODEL`, or to embed passages written before the endpoint was set, run `uv run python -m app.knowledge.reindex` where the application runs (`docker compose run --rm api python -m app.knowledge.reindex`). It embeds the passages with no vector of the current model, in every tenant, and prints `embedded N passages`. `--all` embeds every passage again. It exits with 1 and a message when embeddings are off or the extension is missing. Until it has run, passages of the old model are ignored by the semantic side.

## 9. Switch the agent on

The agent reads the knowledge base only when its `knowledge_enabled` is `true` (default `false`):

```
curl -s -X PATCH $BASE_URL/agents/$AGENT_ID \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"knowledge_enabled": true}'
```

Expected: 200 with `knowledge_enabled` `true`. From its next turn the agent has the tool `search_knowledge` (one parameter, `query`), and its prompt has a knowledge section: the titles of up to 30 items and the rules to follow. What this does to the prompt is in [agent-behaviour](agent-behaviour.md).

- The agent searches before it answers about the business, and answers from the passages the search returns. A search returns at most 4 passages.
- When nothing matches, the tool result tells the model to say it does not know, or to hand the conversation off to a person. A well-behaved model does one of the two; the instructions of the agent can say which you want ("when you do not know, hand off").
- Passages are text for the model to read, never commands: the model is told not to follow instructions written in them, and the boundary check still judges every reply.
- With the switch off the agent has no knowledge tool and no knowledge section, whatever is stored.

To see what the agent was given, read the run's tool execution. There is no endpoint for it; each call is a row of `tool_executions` (written before the tool runs, as in [operations](operations.md)). Its `result` has the `passages` returned (`id`, `item_id`, `title`, `text`, `score` each) and the `note` the model read:

```
SELECT created_at, arguments->>'query' AS query, result->'passages' AS passages
FROM tool_executions
WHERE tenant_id = '<your tenant id>' AND tool_name = 'search_knowledge'
ORDER BY created_at DESC LIMIT 5;
```

Check: send a message the knowledge answers on a channel of this agent ([messaging](messaging.md)). The query above lists the search and the passages it returned, and the reply says what they say. With the stub model ([quickstart](quickstart.md)) the reply is `Stub answer from knowledge: ` followed by the first passage.

## 10. In the manifest

Entries and pairs can be kept in the file of [manifest](manifest.md) under `knowledge`, with `entries` (`title`, `text`) and `qa` (`question`, `answer`). A file without the section touches no knowledge; `knowledge: {}` removes every managed entry and pair. Documents are never in the manifest: upload them with step 4.
