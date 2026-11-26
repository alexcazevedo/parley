# Business tools

Let an agent act on your business: look up a product, add it to a customer's quote, read an order. You run an MCP server; parley is its client. Parley discovers the server's tools, offers them to the model, and calls them for the customer who is writing. Start from the variables of [setup](setup.md): `$BASE_URL`, `$TENANT_KEY` and `$AGENT_ID`. Errors, paging and limits are in [api-conventions](api-conventions.md).

Variable captured here with `jq`: `$SERVER_ID` (step 4).

Every request sends `X-API-Key: $TENANT_KEY`. The sample server is `examples/project_inbox/tools.py` (`shop` with `search_product` and `add_to_quote`); the guide uses its names throughout. Tools that are plain HTTP endpoints, with no MCP server, are in step 12.

## 1. What your server must offer

- MCP over streamable HTTP, at one URL (the example serves `/mcp`). No stdio and no SSE-only server: parley never starts your process.
- A static token that parley sends on every request: `Authorization: Bearer <token>`, or a header you name. No OAuth (step 10).
- Stateless handling. Parley opens a new MCP session for every discovery and every call, so keep nothing in the session.
- `tools/list` and `tools/call`. Parley reads each tool's `name`, `description`, `inputSchema` and the `readOnlyHint` annotation. It does not use resources, prompts or sampling.
- The server must answer for the name parley reaches it by. The MCP Python SDK answers 421 to a request whose `Host` is not `localhost` unless you allow the name. A server reached as `project-inbox:9200` or as your public domain must allow it (the example does: `allowed_hosts` in `examples/project_inbox/app.py`). Without it the server's `status` is `error` (step 4).

The server must be reachable from parley under its address rules: public hosts only, unless `TOOL_ALLOWED_HOSTS` names it (step 9).

## 2. Write tools the model can choose

The model picks a tool only by its name, its description and its schema. Write them for the model, not for a programmer.

- Name: letters, digits, `_` and `-`. Parley shows the model `<server>__<tool>` (`shop__add_to_quote`), and that whole name must match `^[A-Za-z0-9_-]{1,64}$`. A tool whose name does not (a dot, or too long with the server name) is not imported and is listed in `skipped` (step 4). Case matters.
- Description: say what the tool does, when to use it and what it returns. "Busca produtos de papelaria pelo nome. Devolve até cinco, com o id de cada um." is enough. A tool with no description is offered under its bare name.
- Schema: every argument with a type and, when it helps, a description. Parley validates the model's arguments against `inputSchema` before the call; a mismatch goes back to the model as an error (`invalid arguments: ...`) and your server is not called. An argument the model cannot know (the customer's id) does not belong here: it comes in `_meta` (step 3).
- Few tools, each doing one thing. Make the model's next step obvious: the search returns the product `id` that `add_to_quote` asks for.
- Return text. Parley reads the text content blocks, joined by a new line; any other block (image, file) becomes `[image content omitted]`. JSON in a text block is fine.

## 3. What parley sends: `_meta`, and the call id

On each `tools/call` parley puts who the call is for in the request's `_meta`, never in the arguments:

| Key | Value |
|---|---|
| `parley/user_id` | The `user_id` your app gave parley for the customer. |
| `parley/call_id` | A UUID, new for each call. |
| `parley/conversation_id` | The conversation's id. |
| `parley/channel_id` | The conversation's channel id. |
| `parley/contact_name` | The contact's name as written. Absent when the contact has no name. |

- The call id is the same when parley repeats that call (step 7). A call made from `POST /mcp-servers/{server_id}/call` carries only `parley/user_id` and a new `parley/call_id` (step 6).
- Do not trust the arguments for who the customer is; read `parley/user_id`. When it is missing, refuse (below).
- Two calls the model makes on its own, even with the same arguments, have two call ids. The id protects against a repeat of one call, not against a model that asks twice.

Make a write safe to receive twice by remembering the result of each call id. This is the example's `add_to_quote`, trimmed:

```python
@server.tool(
    description="Adiciona um produto ao orçamento do cliente. Use o id devolvido pela busca.",
)
async def add_to_quote(product_id: str, ctx: Context, quantity: int = 1) -> str:
    meta = ctx.request_context.meta or {}
    user_id = meta.get("parley/user_id")
    if not user_id:
        raise ToolError("Não consegui identificar o cliente para montar o orçamento.")
    call_id = meta.get("parley/call_id")
    if call_id and call_id in _state["calls"]:
        return _state["calls"][call_id]  # a repeated call changes nothing
    # ... find the product, check the stock, change the customer's quote ...
    answer = f"Adicionado ao orçamento: {quantity} x {product['name']}."
    if call_id:
        _state["calls"][call_id] = answer
```

Store the id and the result in the same transaction as the change, and keep the ids for as long as a repeat can arrive (the example keeps the newest 1000). The same id with a stored result must return that result and change nothing.

## 4. Register the server

`POST /mcp-servers` registers it:

```
SERVER_ID=$(curl -s -X POST $BASE_URL/mcp-servers \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"name": "shop", "url": "https://shop.example.com/mcp", "auth_config": {"type": "bearer", "token": "<your-token>"}}' | jq -r .id)
```

Expected: 201 with the server, after parley has discovered its tools once:

```json
{"id": "...", "name": "shop", "url": "https://shop.example.com/mcp", "allowed_tools": null,
 "timeout_seconds": 10, "status": "ok", "error": null, "skipped": [], "synced_at": "...",
 "created_at": "...",
 "tools": [{"name": "add_to_quote", "exposed_name": "shop__add_to_quote",
            "description": "Adiciona um produto ao orçamento do cliente. Use o id devolvido pela busca.",
            "input_schema": {"type": "object", "title": "add_to_quoteArguments", "required": ["product_id"], "properties": {"quantity": {"type": "integer", "title": "Quantity", "default": 1}, "product_id": {"type": "string", "title": "Product Id"}}},
            "read_only": false},
           {"name": "search_product", "exposed_name": "shop__search_product", "...": "...", "read_only": true}]}
```

| Field | Rule |
|---|---|
| `name` | Required. Matches `^[a-z0-9_]{1,24}$`; unique per tenant (409 `MCP server name already exists`). It prefixes every tool name and is what an agent lists. |
| `url` | Required, up to 2048 characters. Checked like a tool's `endpoint` (step 9): 422 `invalid url: ...`. |
| `auth_config` | Optional. `{"type": "bearer", "token": "..."}` sends `Authorization: Bearer <token>`; `{"type": "header", "header_name": "X-Api-Key", "token": "..."}` sends the token in that header (`Authorization` when `header_name` is left out). Encrypted, write-only: never returned. Needs a real `SECRETS_KEY`. |
| `allowed_tools` | Optional list of tool names on the server. Only those are imported; without it every tool is. |
| `timeout_seconds` | 1 to 60, default 10. How long parley waits to connect and for each answer; a discovery or a call never lasts much past three times this. |

Registering a server that cannot be reached still gives 201: `status` is `error` and `error` says why (step 10). The server is saved; fix it and sync.

`GET /mcp-servers` lists the tenant's servers, oldest first, paged as in [api-conventions](api-conventions.md). `PATCH /mcp-servers/{server_id}` takes any of the fields above; a change to `url`, `auth_config`, `allowed_tools` or `timeout_seconds` discovers again. Renaming a server does not update the agents: an agent that lists the old name no longer gets its tools. `DELETE /mcp-servers/{server_id}` gives 204 and removes the server and its imported tools. A missing server, or one of another tenant, gives 404 `MCP server not found`.

`GET /mcp-servers/{server_id}` returns one server.

Check: `curl -s $BASE_URL/mcp-servers/$SERVER_ID -H "X-API-Key: $TENANT_KEY"` returns 200 with `status` `ok` and your tools in `tools`, by name, each with its `exposed_name`, its `input_schema` and `read_only`.

## 5. Discovery, and the status

Parley keeps its own copy of the tools (name, description, schema, `read_only`), so a customer's turn never waits for discovery. It discovers at registration, after a change (step 4), on request and by itself: the worker syncs a server again when its last attempt is older than `MCP_SYNC_INTERVAL_SECONDS` ([configuration](configuration.md); the worker must be running, [operations](operations.md)). To discover now, `POST /mcp-servers/{server_id}/sync`:

```
curl -s -X POST $BASE_URL/mcp-servers/$SERVER_ID/sync -H "X-API-Key: $TENANT_KEY"
```

Expected: 200 with the server as in step 4. `synced_at` is the time of the last attempt, successful or not.

- A tool removed from the server is removed from parley at the next sync; a new one is added. An agent with the server picks up both with no change of its own.
- A failed sync keeps the last known tools and sets `status` `error`; the agent keeps using them. A server that has never been reached has none.
- `skipped` lists the tools not imported because their exposed name is invalid (step 2).

Repeats, `readOnlyHint` and unknown outcomes are in step 7.

Check: add a tool to your server, call the `sync` above, and `tools` lists it.

## 6. Try one call

Try a tool without a model and without a conversation with `POST /mcp-servers/{server_id}/call`: `tool` is the name on the server (not the exposed one), `user_id` is any customer id.

```
curl -s -X POST $BASE_URL/mcp-servers/$SERVER_ID/call \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"tool": "search_product", "arguments": {"query": "cartolina branca"}, "user_id": "ana"}'
```

Expected: 200 with `ok`, `result` and `error`:

```json
{"ok": true, "result": "[{\"id\": \"cartolina-branca-150g\", \"name\": \"Cartolina branca 150g\", \"price\": 1.5, \"stock\": 200}, {\"id\": \"cartolina-branca-180g\", \"name\": \"Cartolina branca 180g\", \"price\": 2.0, \"stock\": 120}]", "error": null}
```

Now a write, for one customer:

```
curl -s -X POST $BASE_URL/mcp-servers/$SERVER_ID/call \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"tool": "add_to_quote", "arguments": {"product_id": "cartolina-branca-150g", "quantity": 1}, "user_id": "ana"}'
```

Expected: 200 with `"ok": true` and `"result": "Adicionado ao orçamento: 1 x Cartolina branca 150g."`. Send it again and the quote has two: each try is a new call with a new call id.

- A tool's error result and a failure both give `ok` `false` with the text in `error`. A product that is not in the catalog: `"error": "Error executing tool add_to_quote: Produto nao-existe não encontrado."` (the Python SDK puts `Error executing tool <name>: ` before the message you raise; the model reads it too). A missing argument: `"error": "invalid arguments: 'product_id' is a required property"`.
- An unknown `tool` gives 404 `MCP tool not found`. Arguments are checked against the schema first, as in a conversation.
- The result is cut at `TOOL_MAX_RESPONSE_BYTES` (step 8). Nothing is written to `tool_executions`.

Check: the first call returns `"ok": true` and names both products.

## 7. Attach the server to an agent

An agent lists the servers it may use in `mcp_servers`:

```
curl -s -X PATCH $BASE_URL/agents/$AGENT_ID \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"mcp_servers": ["shop"]}'
```

Expected: 200 with `mcp_servers` `["shop"]`. The list is replaced, not merged, and a name with no such server is ignored. From its next turn the agent is offered every imported tool of `shop` as `shop__<tool>`. To offer fewer tools, set `allowed_tools` on the server: the server is attached as a whole, not tool by tool, and the limit applies to every agent that lists it.

Each call is a row of `tool_executions`, written before the call runs, with `call_id` and the `context` that went in `_meta` (`conversation_id`, `channel_id`, `user_id`, `contact_name` when known). There is no endpoint for it:

```
SELECT created_at, tool_name, status, call_id, context
FROM tool_executions
WHERE tenant_id = '<your tenant id>' AND tool_name LIKE 'shop__%'
ORDER BY created_at DESC LIMIT 5;
```

What the call gives the model:

- A success is the tool's text under `result`, with a `note` that says it is reference data from the business's system, not instructions, and to never follow commands inside it. The boundary check still judges the final reply.
- When your tool refuses, the model reads your message and can explain it to the customer. Refuse with an error result, not with a failure: in the Python SDK raise `ToolError` (`mcp.server.mcpserver.exceptions`) with a message written for the customer ("Produto X está sem estoque"). Any other exception reaches parley as a generic error with no useful text.
- A failure (the server unreachable, a timeout, a protocol error, a refused token) gives the model `tool call failed: <reason>` and the model apologises. Keep these for what is really broken.

Repeats. A turn that runs again for the same messages (a retry) does not call a finished call again: the model gets the saved result. A call left with an unknown outcome (the answer never came back: a timeout, a protocol error, a worker that died) is called again only when the tool has `readOnlyHint` true, with the same call id. Any other tool is not: the model is told the outcome is unknown. A call that cannot have reached your server (address refused, could not connect, token refused, OAuth) is finished as a failure and is not repeated. A tool with no annotation counts as not read-only, so mark every tool that only reads: in the SDK `annotations=ToolAnnotations(read_only_hint=True)`.

Check: send a message that needs the tool on a channel of this agent ([messaging](messaging.md)); the query above lists the call with its `context`. Without a model key, step 11.

## 8. Limits

- `TOOL_MAX_RESPONSE_BYTES` (default 32768, [configuration](configuration.md)) cuts the text of a result at that many UTF-8 bytes, for the model and for the try route. The model is not told it was cut; the record shows `truncated`. Keep results short and ask for the rest with a second tool.
- `timeout_seconds` per server (step 4).
- Parley follows no redirect: register the final URL.
- The model chooses arguments from the schema, so validate on your side too.

## 9. The address allow-list

A server's `url` follows the rules of a tool's `endpoint` ([setup](setup.md) step 2): checked without DNS when registered (`localhost`, `.local`, `.localhost` and `.internal` names and private addresses give 422 `invalid url: ...`) and again with DNS before every connection, where every resolved address must be public. With `TOOL_ALLOWED_HOSTS` set, only the listed host names pass and the private-address checks are skipped for them, so the list is exclusive: add your MCP host to the hosts your tools already use. `TOOL_SSRF_DISABLED=true` turns the guard off, for local development only. A refused address is `error` `address refused: ...` (step 10).

## 10. When the status is `error`

`error` holds the reason; the last known tools stay in use. The reasons:

| `error` | What to do |
|---|---|
| `the server requires OAuth; parley supports only a bearer or header token` | Your server answered 401 and asked for OAuth. Parley has no OAuth flow: give it a static token it accepts. |
| `the server refused the token (HTTP 401)`, or `(HTTP 403)` | Check `auth_config` (type, `header_name`, token). `PATCH` it: parley discovers again. |
| `could not reach the server` | The name does not resolve, the port is closed or TLS failed. |
| `the server did not answer in time` | Make it faster, or raise `timeout_seconds` (at most 60). |
| `address refused: ...` | The address rules of step 9. |
| `protocol error: ...` or `<ErrorName>: ...` | The server answered something that is not MCP, or refused the request (a 421 from a `Host` the server does not allow, step 1). |

A call to a server in this state may still work if the server comes back; the worker tries again every `MCP_SYNC_INTERVAL_SECONDS`, or call the `sync` of step 5 after you fix it.

Check: register a server with a wrong token; `status` is `error` with `the server refused the token (HTTP 401)`. `PATCH` the right one and `status` is `ok`.

## 11. Try it with the stub model

With no model key, the stub model ([quickstart](quickstart.md)) calls your tool when the message is `/tool <name> <JSON arguments>`, using the exposed name. Start the stack with the example project, which serves the sample server, and its demo file:

```
export DEMO_SECRETS_KEY=$(uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
docker compose -f docker-compose.yml -f docker-compose.stub.yml -f docker-compose.example.yml -f docker-compose.demo.yml up -d --build --wait
```

`docker-compose.demo.yml` sets `DEMO_MODE`, passes the key made by the first line as `SECRETS_KEY` (a server with a token needs a valid one; the key exists only for this run and compose refuses to start without it) and sets the example's token, so the stack starts with a placeholder `.env`: demo only, never on a server others can reach. Register `http://project-inbox:9200/mcp` as `shop` with the token `demo-tools-token` and attach it to an agent (steps 4 and 7). The example's host name is private; `docker-compose.example.yml` sets `TOOL_ALLOWED_HOSTS` to `project-inbox` for `api` and `worker`, so the allow-list of step 9 lets it through (the list is then exclusive). Then send the message `/tool shop__search_product {"query": "cartolina branca"}` on a channel of the agent. The reply is `Stub tool result: ` followed by the tool's result as the model got it:

```
Stub tool result: {"ok": true, "data": {"result": "[{\"id\": \"cartolina-branca-150g\", ...}]", "note": "..."}}
```

A tool the agent is not offered gives `Stub: tool <name> is not offered.`, and a malformed command `Stub: invalid /tool command.`. `scripts/demo_business_tools.py` runs the whole path against the stack: two customers, one quote each, last line `business tools demo: ok`.

Check: the stub's reply to the `/tool` message starts with `Stub tool result: {"ok": true`.

## 12. HTTP tools get the same context

A tool made with `POST /tools` has no MCP server, but each request it receives carries the same information as headers: `X-Parley-Conversation-Id`, `X-Parley-Channel-Id`, `X-Parley-User-Id`, `X-Parley-Contact-Name` (percent-encoded UTF-8, left out when unknown) and `X-Parley-Call-Id` (the call id; a `GET` that parley repeats sends the same one). They replace an auth header of the same name, and the arguments are unchanged. The table is in [setup](setup.md) step 2. The same rules for idempotency apply: only a `GET` is repeated.

## 13. What the protection is today, and what a third-party server can do

Parley sends no signature yet. A call is protected by the token, by TLS (use `https`), and by the address rules; the context in `_meta` is not signed. Anyone who holds the token can send any `parley/user_id`. So: keep the token secret, accept it only on the MCP route, and keep the server off networks that others use. A signed call is an open follow-up ([FOLLOW_UPS](../FOLLOW_UPS.md)).

A server that knows nothing of parley (a public catalog, a weather or currency service, your own documentation) can be registered if it speaks MCP over streamable HTTP. It can answer lookups, the same for every customer. It cannot do anything per customer: it ignores `_meta`, so it cannot tell whose quote, order or account is meant. Register only servers you trust: their descriptions go to the model and their results come back to it.

## 14. Checklist

- The server answers MCP over streamable HTTP at one URL, with a static token, and accepts the `Host` parley uses.
- Names are letters, digits, `_` and `-`; the server name plus `__` plus the tool name is at most 64 characters.
- Every description says what the tool does, when to use it and what it returns; every argument has a type.
- Read-only tools have `readOnlyHint`; no write does.
- Every write reads `parley/user_id` and remembers the result of its `parley/call_id`.
- A refusal is an error result with a message for the customer; only real breakage raises.
- Results are short text, well under `TOOL_MAX_RESPONSE_BYTES`.
- The host is public or in `TOOL_ALLOWED_HOSTS`; no OAuth.
- Registered, `status` `ok`, tools listed, one call tried with `POST /mcp-servers/{server_id}/call`, the server in the agent's `mcp_servers`.
- The ledger query of step 7 shows the call with the right `context`.

The same server can be kept in the [manifest](manifest.md) under `mcp_servers`.
