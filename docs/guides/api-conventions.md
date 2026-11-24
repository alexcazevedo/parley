# API conventions

Rules that hold for every endpoint. Request bodies are JSON with `Content-Type: application/json`. Ids are UUIDs. Times are ISO 8601 with a time zone.

## Authentication

| Header | Used on | Value |
|---|---|---|
| `X-Admin-Key` | `POST /tenants` only | The `ADMIN_API_KEY` setting. |
| `X-API-Key` | every other tenant endpoint | The `api_key` returned once by `POST /tenants`. |
| `apikey` | `POST /webhooks/whatsapp/{channel_id}` | The channel `secret`. Sent by Evolution, not by your code. |

A tenant key only reaches that tenant's data. An id from another tenant gives 404, the same as an id that does not exist.

No key needed: `GET /health`, `GET /ready`, `GET /openapi.json`, `GET /llms.txt` and the guides it links to.

Check: `curl -s -o /dev/null -w '%{http_code}' $BASE_URL/agents` prints 401. The same with `-H "X-API-Key: $TENANT_KEY"` prints 200.

## Errors

Every error from an endpoint has this shape:

```
{"error": {"code": "http_error", "message": "agent not found", "request_id": "..."}}
```

- `code` is `http_error` (a refused request), `validation_error` (422 for a bad body, query or path value) or `internal_error` (500, message `An internal error occurred`).
- `message` is for people. Decide in code by the status and `code`, not by the text.
- `request_id` equals the `X-Request-ID` response header. Quote it when reporting a problem.
- A `validation_error` adds `details`, a list of `{"field": "...", "message": "..."}`. `field` is the body or query field, with nested parts joined by dots (for example `boundary_never.0`). The rejected value is never echoed.

Two cases do not use this shape and return only `{"detail": "..."}`: an unknown path (404 `Not Found`) and a wrong method on a known path (405 `Method Not Allowed`).

Status codes you will meet:

| Status | When |
|---|---|
| 200 | Success. |
| 201 | Created. |
| 204 | Deleted, no body. |
| 401 | Missing or wrong `X-Admin-Key`, `X-API-Key` or webhook `apikey`. |
| 404 | The id does not exist for your tenant. |
| 409 | A unique name or pair already exists (tool name, category name, contact), or the move is not allowed in the conversation's state (claim, release, resume, handoff). |
| 413 | Message text over `MAX_MESSAGE_CHARS`, or an upload over `MEDIA_MAX_BYTES`. |
| 415 | Upload content that is not a supported image or audio type. |
| 422 | Invalid body, query or cursor; or a reference to an agent or channel that is not yours. |
| 429 | Inbound message rate limit exceeded. |
| 500 | Server error. |
| 502 | `POST /conversations/{conversation_id}/messages` only: the channel's address is refused (`channel endpoint refused`). |
| 503 | `GET /ready` only: Postgres or Redis is not answering. |

Check: `curl -s $BASE_URL/agents/00000000-0000-0000-0000-000000000000 -H "X-API-Key: $TENANT_KEY"` returns 404 with `error.message` `agent not found`. `curl -s -X POST $BASE_URL/agents -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" -d '{}'` returns 422 with `details` naming `name`, `instructions` and `model`.

## Request ids

Send `X-Request-ID` to trace a request. It is kept when it matches `[A-Za-z0-9._-]{1,64}`; otherwise parley ignores it and generates a UUID. The response always carries the id in `X-Request-ID`, and it appears in the logs of that request.

Check: `curl -si -H "X-Request-ID: trace-1" $BASE_URL/health` includes `x-request-id: trace-1`.

## Paging

These list endpoints page: `GET /agents`, `GET /tools`, `GET /channels`, `GET /contacts`, `GET /categories`, `GET /conversations` and `GET /conversations/{conversation_id}/messages`.

- `limit`: rows per page. Default 100, 1 to 500 (422 outside that).
- `cursor`: the value of the previous response's `X-Next-Cursor` header. Treat it as opaque. A malformed one gives 422 `invalid cursor`.
- `X-Next-Cursor`: present only when more rows remain. Absent on the last page. Repeat the request with `cursor` set to it, keeping the same other parameters.

```
curl -si "$BASE_URL/agents?limit=1" -H "X-API-Key: $TENANT_KEY"
curl -s "$BASE_URL/agents?limit=1&cursor=<X-Next-Cursor value>" -H "X-API-Key: $TENANT_KEY"
```

Lists are oldest first (`GET /categories`: by name), except `GET /conversations` (most recent activity first) and messages (oldest first within a page; the first page without `after_seq` or `after` is the newest `limit` messages). Messages also take `after_seq` and `after` for polling; see [messaging](messaging.md). `GET /lines` has no paging: `limit` (default 200, 1 to 500) caps the total and keeps the oldest.

Check: with two or more agents, the first request above returns one agent and an `X-Next-Cursor` header, and the second returns the next one.

## Limits

- Message text: at most `MAX_MESSAGE_CHARS` (default 4000). Over it, `POST /channels/webchat/messages` gives 413. WhatsApp webhook text is cut to the limit, not refused.
- Rate: inbound messages are counted per customer on a channel per minute (`RATE_LIMIT_USER_PER_MIN`) and per tenant per minute (`RATE_LIMIT_TENANT_PER_MIN`). Over either, `POST /channels/webchat/messages` gives 429 and nothing is stored. Wait for the next minute. The customer limit is counted first: a customer already over their own limit does not use up the tenant's. The WhatsApp webhook never answers 429: it stores the message, runs no agent turn and answers `{"status": "rate_limited"}`.
- Upload size: `MEDIA_MAX_BYTES`.

Defaults and the other settings are in [configuration](configuration.md).

Check: `curl -s $BASE_URL/openapi.json | jq '.paths | length'` prints a number above 0.

## OpenAPI

`GET /openapi.json` is the machine-readable schema of the running instance, and lists every path, field and status above. It needs no key.
