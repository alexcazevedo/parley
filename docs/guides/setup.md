# Setup

Create a tenant, then an agent, a tool and a channel. Start from a healthy stack ([quickstart](quickstart.md)). To set up agents, departments, tools and channels from one file instead, create the tenant here (step 1) and continue in the [manifest](manifest.md) guide; the calls below make the same items one at a time. Errors, paging and headers are in [api-conventions](api-conventions.md).

Variables used below: set `BASE_URL` and `ADMIN_API_KEY` yourself; the rest are captured from responses with `jq`, as `NAME=$(curl ... | jq -r .field)`. Commands need `curl` and `jq`. Run them in one shell session, so the variables stay set. Use the port from `API_PORT` in `BASE_URL` if you changed it ([quickstart](quickstart.md) step 3).

```
BASE_URL=http://localhost:8001
ADMIN_API_KEY=<the ADMIN_API_KEY value from .env>
```

Every request with a body sends `Content-Type: application/json`.

## 1. Create a tenant

`POST /tenants` with header `X-Admin-Key` set to the admin key.

```
TENANT=$(curl -s -X POST $BASE_URL/tenants \
  -H "X-Admin-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"name": "acme"}' | jq .)
TENANT_ID=$(echo "$TENANT" | jq -r .id)
TENANT_KEY=$(echo "$TENANT" | jq -r .api_key)
```

Expected: 201 with `id`, `name` and `api_key` (in `$TENANT`). The `api_key` is shown once and cannot be read again: `$TENANT_KEY` is the only copy, so store it. `$TENANT_ID` is the `id`. If either variable is empty or `null`, print `echo "$TENANT"` to read the error.

Every request below sends `X-API-Key: $TENANT_KEY`. Each tenant sees only its own data.

Check: `curl -s $BASE_URL/tenants/me -H "X-API-Key: $TENANT_KEY"` returns 200 with the same `id` and `name`. A wrong admin key gives 401 `invalid admin key`.

## 2. Create a tool (optional)

Create tools before the agent that uses them. Skip this step for an agent with no tools of its own.

`POST /tools` fields:

| Field | Default | Rule |
|---|---|---|
| `name` | required | Matches `^[A-Za-z0-9_-]{1,64}$`. Unique per tenant (409 `tool name already exists`). `remember` and `handoff_to_human` are reserved. |
| `description` | `null` | Up to 2000 characters. The model reads it to decide when to call the tool. |
| `input_schema` | `{}` | A JSON Schema object for the arguments the model passes. |
| `endpoint` | required | The URL parley calls. `http` or `https` only. |
| `method` | `POST` | `GET`, `POST`, `PUT`, `PATCH` or `DELETE`, any case; stored upper case. |
| `auth_config` | `null` | See below. Write-only: no response ever returns it. |
| `timeout_seconds` | `10` | 1 to 60. |

Arguments from the model go in the query string for `GET` and `DELETE`, and in a JSON body for `POST`, `PUT` and `PATCH`. Redirects are not followed. Tool requests send `Accept-Encoding: identity`; a response with any other `Content-Encoding` is a failed result (`unsupported response encoding`).

Every tool request also carries who the call is for, in headers. The arguments, query string and body are not changed. To give the agent a whole set of tools from one MCP server, with discovery, see [business-tools](business-tools.md); the agent field for it is `mcp_servers`.

| Header | Value |
|---|---|
| `X-Parley-Conversation-Id` | The conversation id. |
| `X-Parley-Channel-Id` | The conversation's channel id. |
| `X-Parley-User-Id` | The contact's `user_id`: the id your app gave parley for that customer. |
| `X-Parley-Contact-Name` | The contact's name, percent-encoded (UTF-8). Left out when the contact has no name. |
| `X-Parley-Call-Id` | A UUID, new for each call. Retries of a `GET` inside one call, and a repeat of a `GET` whose first outcome is unknown, send the same id. |

The same values are stored on the call's record. If `header_name` below is one of these names, the context header is sent instead of the token.

`auth_config` is an object with `type` and `token`. `{"type": "bearer", "token": "..."}` sends `Authorization: Bearer ...`. `{"type": "header", "token": "...", "header_name": "X-Key"}` sends the token in that header (`Authorization` when `header_name` is omitted). Any other `type` sends no auth. It is encrypted with `SECRETS_KEY`; with a placeholder key the request fails with 500.

The endpoint must pass the address guard (422 `invalid endpoint: ...` otherwise):

- Default: `localhost`, names ending `.local`, `.localhost` or `.internal`, and private, loopback and link-local addresses are refused. Public hosts pass.
- The check at create is by name, without DNS. It also refuses an IP literal in any IPv4 notation (`127.1`, `0x7f000001`) and any IPv6 address that is not globally routable (`http://100.64.0.1/x` is refused). Other host names pass at create; the name is resolved when the tool is called, and a host that resolves to a private address is refused then.
- With `TOOL_ALLOWED_HOSTS` set: only those host names pass and every other host is refused, so the list is exclusive. Set it in `.env` (a comma-separated list); `docker-compose.yml` passes it to `api` and `worker`, and the shipped `.env.example` leaves it empty. Restart with `docker compose up -d` after a change. The bundled `mock-tools` service is a private host, so it is refused until `.env` has `TOOL_ALLOWED_HOSTS=mock-tools`; with that line, public tool hosts are refused too, so list them as well (`TOOL_ALLOWED_HOSTS=mock-tools,api.example.com`).
- `TOOL_SSRF_DISABLED=true` turns the guard off. Local development only.

```
TOOL_ID=$(curl -s -X POST $BASE_URL/tools \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"name": "get_order", "description": "Look up an order by id",
       "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]},
       "endpoint": "http://mock-tools:9000/orders", "method": "GET"}' | jq -r .id)
```

Expected: 201 with `id`, `name`, `description`, `input_schema`, `endpoint`, `method`, `timeout_seconds`, `created_at`. `$TOOL_ID` is the `id`; use it to edit or delete the tool.

`http://mock-tools:9000/orders` is created in any case (a host name passes at create), but it is called only with `TOOL_ALLOWED_HOSTS=mock-tools` in `.env` (see above); without it the call is refused because the name resolves to a private address. It works only inside the compose network: the `mock-tools` service has no host port, so `curl` from your host cannot reach it. (`docker compose ps` shows `8000/tcp` for it; that is the image's default and not the port the mock listens on, which is 9000.) Only `api` and `worker` call it. The stub model never calls tools other than `handoff_to_human` ([quickstart](quickstart.md)), so the tool path needs a real model key.

Check: `curl -s $BASE_URL/tools -H "X-API-Key: $TENANT_KEY"` lists it, and the response has no `auth_config`.

## 3. Create an agent

`POST /agents`. Only `name`, `instructions` and `model` are required. Expected: 201.

| Field | Default | Bounds | Meaning |
|---|---|---|---|
| `name` | required | 1 to 255 characters | Label for the agent. |
| `instructions` | required | none | System prompt. |
| `provider` | `openai_compatible` | up to 50 characters | One of `openai_compatible` (the server at `LLM_BASE_URL`, with `LLM_API_KEY`), `openai` (needs `OPENAI_API_KEY`), `anthropic` (needs `ANTHROPIC_API_KEY`) or `google` (needs `GOOGLE_API_KEY`). Any other value, or a native provider whose key is not set on the server, is refused with 422 naming the problem: `unknown provider: use openai_compatible, openai, anthropic or google`, or `provider 'anthropic' is not configured on this server: ANTHROPIC_API_KEY is not set`. An agent already stored with a provider the server no longer serves is left as it is until a turn runs. |
| `model` | required | 1 to 255 characters | Model name sent to the provider, in that vendor's own naming, for example `gpt-4o-mini`. |
| `temperature` | `0.7` | 0 to 2 | Sampling temperature. |
| `max_tokens` | `1024` | 1 to 32768 | Longest model reply. |
| `max_tool_iterations` | `5` | 1 to 20 | Most model calls in one turn; each may call tools. |
| `memory_enabled` | `true` | | Gives the agent the built-in `remember` tool and long-term memories. |
| `handoff_enabled` | `true` | | Gives the agent the built-in `handoff_to_human` tool. |
| `knowledge_enabled` | `false` | | Gives the agent the built-in `search_knowledge` tool, which reads the tenant's knowledge base. Fill it first: [knowledge](knowledge.md). |
| `tool_names` | `[]` | | Names of your tools this agent may call. Not checked at creation: a name with no such tool is silently never called. |
| `context_message_limit` | `20` | 1 to 200 | Recent messages sent to the model each turn. |
| `summary_threshold` | `40` | 0 to 1000 | Message count that triggers a summary of older messages. `0` turns summaries off. |
| `boundary_scope` | `null` | up to 2000 characters | What the agent may talk about. See [agent-behaviour](agent-behaviour.md). |
| `boundary_never` | `[]` | up to 50 items, each 1 to 300 characters | Things the agent must never do. |
| `boundary_refusal_message` | `null` | 1 to 1000 characters | Text sent when a reply is blocked. |
| `boundary_check_enabled` | `true` | | Check each reply against the boundaries. |
| `sentiment_escalation_enabled` | `false` | | Hand over to a human when the customer's frustration score reaches `SENTIMENT_THRESHOLD`. |
| `handoff_message` | `null` | 1 to 1000 characters | Text sent on handoff. `null` stores the default `Vou te transferir para um atendente humano.` |
| `error_message` | `null` | 1 to 1000 characters | Text sent when a turn fails. `null` uses the built-in text. |
| `unsupported_media_message` | `null` | 1 to 1000 characters | Text sent when the customer sends media that cannot be read. `null` uses the built-in text. |

An out-of-range value gives 422 `validation_error`; `details` names the field.

### Attach tools

Put the tool names from step 2 in `tool_names`. Repeat `PATCH /agents/{agent_id}` with `{"tool_names": [...]}` to change them later: the list is replaced, not merged.

```
AGENT_ID=$(curl -s -X POST $BASE_URL/agents \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"name": "support", "instructions": "You are a support agent for Acme. Answer briefly.",
       "model": "gpt-4o-mini", "tool_names": ["get_order"]}' | jq -r .id)
```

Expected: 201 with every field above plus `id` and `created_at`. `$AGENT_ID` is the `id`.

Check: `curl -s $BASE_URL/agents/$AGENT_ID -H "X-API-Key: $TENANT_KEY"` returns 200 and `tool_names` lists the tool. An unknown id gives 404 `agent not found`.

## 4. Create a channel

`POST /channels` fields:

| Field | Default | Meaning |
|---|---|---|
| `type` | required | `webchat`, `whatsapp_evolution` or `external` (a channel the project owns: see [project-channel](project-channel.md)). |
| `name` | required | Label. |
| `agent_id` | required | `$AGENT_ID`. An unknown or foreign id gives 422 `agent not found`. |
| `config` | `{}` | Object, returned in every response, so put no secrets in it. |
| `secret` | `null` | Write-only, encrypted with `SECRETS_KEY`, never returned. |

`config` keys:

- `humanize` (any type): `true` splits a reply into several short messages with typing pauses. Tuned by the `HUMANIZE_*` settings in [configuration](configuration.md).
- `whatsapp_evolution` also needs `base_url` (the Evolution server, public address, or a host listed in `CHANNEL_ALLOWED_HOSTS`) and `instance` (the Evolution instance name). Neither is checked for presence at creation; a missing one fails when a reply is sent. A bad `base_url` gives 422 `invalid base_url: ...`.
- `external` takes `events_url`, where parley pushes events; the response to the create call also holds `events_secret`, shown once. Steps and details are in [project-channel](project-channel.md).
- `secret` for `whatsapp_evolution` is the Evolution API key. Parley sends it to Evolution on every call and requires Evolution's webhook to send the same value in the `apikey` header to `POST /webhooks/whatsapp/{channel_id}`. Without a secret every webhook gets 401.

Web chat:

```
CHANNEL_ID=$(curl -s -X POST $BASE_URL/channels \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d "{\"type\": \"webchat\", \"name\": \"site\", \"agent_id\": \"$AGENT_ID\", \"config\": {\"humanize\": true}}" | jq -r .id)
```

WhatsApp through Evolution:

```
curl -s -X POST $BASE_URL/channels \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d "{\"type\": \"whatsapp_evolution\", \"name\": \"whatsapp\", \"agent_id\": \"$AGENT_ID\",
       \"config\": {\"base_url\": \"https://evolution.example.com\", \"instance\": \"acme\"},
       \"secret\": \"<evolution api key>\"}"
```

Expected: 201 with `id`, `type`, `name`, `agent_id`, `config`, `created_at`. No `secret`. `$CHANNEL_ID` is the `id`. To keep the WhatsApp channel's id too, capture it the same way (`... | jq -r .id`) into `$CHANNEL_ID` when that is the channel you use next, or into a name of your own to keep both.

Check: `curl -s $BASE_URL/channels/$CHANNEL_ID -H "X-API-Key: $TENANT_KEY"` returns 200 with `agent_id` equal to `$AGENT_ID`. To point the channel at another agent, `PATCH /channels/{channel_id}` with `{"agent_id": "..."}`.

## 5. Create a contact (optional)

A contact is created automatically when a customer first writes. Create one yourself only to set `display_name` or `attributes` before that, or to attach memories. The example uses `customer-1`, the `USER_ID` that [messaging](messaging.md) sends as, so that customer is this contact.

`POST /contacts` fields: `channel_id` (required), `external_user_id` (required; the `user_id` you send for web chat, the phone number for WhatsApp), `display_name` (default `null`), `attributes` (default `{}`).

```
CONTACT_ID=$(curl -s -X POST $BASE_URL/contacts \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d "{\"channel_id\": \"$CHANNEL_ID\", \"external_user_id\": \"customer-1\", \"display_name\": \"Ana\"}" | jq -r .id)
```

Expected: 201 with `id`, `channel_id`, `external_user_id`, `display_name`, `attributes`, `created_at`. `$CONTACT_ID` is the `id`. A second contact with the same `channel_id` and `external_user_id` gives 409 `contact already exists for this channel`.

Check: `curl -s $BASE_URL/contacts/$CONTACT_ID -H "X-API-Key: $TENANT_KEY"` returns 200.

## Keep these

| Name | From | Used for |
|---|---|---|
| `$TENANT_KEY` | step 1 `api_key` (shown once; no way to read it again) | the `X-API-Key` header on every request |
| `$TENANT_ID` | step 1 `id` | reference only |
| `$AGENT_ID` | step 3 `id` | channels, agent edits |
| `$TOOL_ID` | step 2 `id` | tool edits and deletes |
| `$CHANNEL_ID` | step 4 `id` | sending messages, webhook URL |
| `$CONTACT_ID` | step 5 `id` | memories, contact edits |

The admin key is needed only to create more tenants.

Optional: give the agent facts about the business (hours, prices, policies, documents) in [knowledge](knowledge.md), then switch it on with `knowledge_enabled`.

Next: send messages through the channel in [messaging](messaging.md).
