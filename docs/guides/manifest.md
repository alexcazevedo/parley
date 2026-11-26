# Manifest

Describe a business in one file (agents, departments, tools, channels) and apply it with one call. Apply it again after every edit. Start from the variables of [setup](setup.md): `$BASE_URL` and `$TENANT_KEY`. Errors, paging and limits are in [api-conventions](api-conventions.md).

Variables captured here with `jq`: `$EVENTS_SECRET` (step 5) and `$PROJECT_CHANNEL_ID` (step 6), both from the answer of the apply. Commands that send a file expect it in the current directory.

Every request sends `X-API-Key: $TENANT_KEY`, except `GET /manifest/schema`, which needs no key.

## 1. What the manifest is

The manifest is the record of how a tenant is set up. Parley keeps a list of the items an apply created or adopted (the managed items). Every apply compares the file with the tenant and changes only what differs.

- It is a single document with `version: 1`, sent as JSON or YAML to `POST /manifest/apply`.
- It holds agents, departments, tools and channels. It holds no conversation, contact or memory, and no credential (step 3).
- An apply writes everything or nothing. Items made by hand through the API are never changed or deleted by an apply, unless the file adopts them (step 7).
- The tenant is created first, with the admin key: [setup](setup.md) step 1. A manifest applies to the tenant of the key in `X-API-Key`.
- The manifest does not replace [setup](setup.md): its calls still work and make the same items.

Check: `curl -s $BASE_URL/manifest/schema` returns 200 with the JSON Schema of the file.

## 2. The file

Top-level keys:

| Key | Holds | Identified by |
|---|---|---|
| `version` | required, the number `1` | |
| `agents` | up to 50 agents | `key` |
| `departments` | up to 200 departments (categories, see [operators](operators.md)) | `name` |
| `tools` | up to 200 HTTP tools | `name` |
| `channels` | up to 50 channels | `key` |
| `knowledge` | `entries` and `qa`, up to 500 of each (below) | `title`, `question` |

A key not in this list is refused, in the file and inside every item. Each section is optional.

- `key` is a name you choose, 1 to 64 of `a-z`, `0-9`, `_` and `-`. It is how the file and parley recognise an agent or a channel, because names are not unique. Departments and tools have unique names, so their `name` is the identity.
- `name` is a label. Changing the `name` of an agent or a channel is an update. Changing a `key`, or the `name` of a department or a tool, is a new item and a delete of the old one (step 9).
- An agent takes every field of `POST /agents` ([setup](setup.md) step 3) plus `key`. A department takes `name` and `description`. A tool takes every field of `POST /tools` ([setup](setup.md) step 2). The same rules apply: bounds, the tool name rule, the address check on `endpoint`.
- A channel takes `key`, `type` (`webchat`, `whatsapp_evolution` or `external`), `name`, `agent` (the `key` of an agent in the same file), `config` and `secret`. The `config` rules are those of [setup](setup.md) step 4 and, for `external`, [project-channel](project-channel.md).
- A field left out means its default. The file is the whole state of an item: an apply that finds a field set by hand and left out of the file puts the default back, and lists the field in `fields` (step 4). `config` is replaced as a whole.
- `tool_names` of an agent may name a tool of the file, a tool of the tenant, or a built-in (`remember`, `handoff_to_human`).
- Quote YAML strings that YAML reads as something else: `"no"`, `"on"`, `"1.0"`, `"08:00"`. Use `|` for a persona.

A complete file. Save it as `manifest.yaml`:

```yaml
# file: manifest.yaml
version: 1

agents:
  - key: support
    name: Acme support
    model: gpt-4o-mini
    instructions: |
      You are the support agent of Acme, an online shop. Answer briefly.
      When the customer gives an order number, look it up with get_order.
    tool_names:
      - get_order

departments:
  - name: billing
    description: Invoices, payments and refunds
  - name: shipping
    description: Where an order is, delivery dates and address changes

tools:
  - name: get_order
    description: Look up an order by id
    method: GET
    endpoint: https://api.example.com/orders
    input_schema:
      type: object
      properties:
        order_id:
          type: string
      required:
        - order_id
    auth_config:
      type: bearer
      token:
        secret: orders_token

channels:
  - key: inbox
    type: external
    name: Project inbox
    agent: support
    config:
      events_url: https://inbox.example.com/parley/events
```

### Knowledge

An optional `knowledge` section holds the entries and question-and-answer pairs of the knowledge base. Documents are not in the manifest: they are uploaded through the API.

```yaml
knowledge:
  entries:
    - title: Opening hours
      text: We open Monday to Friday, from 9:00 to 18:00.
  qa:
    - question: How long does delivery take?
      answer: Delivery takes 3 to 5 business days.
```

- `entries` is a list of `title` and `text`; `qa` is a list of `question` and `answer`. Both lists hold at most 500 items, and an unknown key is refused. The key of an entry is its `title`, the key of a pair is its `question`.
- A file without the `knowledge` section changes no knowledge. A file with the section makes it declarative: a managed entry or pair no longer in it is deleted (with its passages), and a hand-made one of the same title is adopted. An item that is created or whose text changes is `processing` until the worker has split it, as with the API. Documents and hand-made items that the file does not name are never touched.
- The limits `KNOWLEDGE_MAX_ITEMS`, `KNOWLEDGE_MAX_ITEM_CHARS` and `KNOWLEDGE_MAX_TOTAL_CHARS` apply to the result of the apply (documents count), before anything is written.

Complete examples for a shop, a clinic and a law firm are in step 10.

## 3. Secrets

The file never holds a credential. It names a secret, and the value is set apart.

```
curl -s -X PUT $BASE_URL/secrets/orders_token \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"value": "token-from-the-orders-system"}'
```

Expected: 204. `PUT /secrets/{name}` creates the secret or replaces its value. The name matches `^[a-z0-9_]{1,64}$`; the value is 1 to 8192 characters. The value is stored encrypted and no endpoint returns it.

`GET /secrets` lists `name`, `created_at` and `updated_at`, sorted by name. `DELETE /secrets/{name}` is 204, 404 `secret not found`, or 409 while a managed item uses the secret (below).

```
curl -s $BASE_URL/secrets -H "X-API-Key: $TENANT_KEY"
curl -s -X PUT $BASE_URL/secrets/scratch \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"value": "x"}'
curl -s -X DELETE $BASE_URL/secrets/scratch -H "X-API-Key: $TENANT_KEY"
```

Expected: 200 with `[{"name": "orders_token", ...}]`, then 204, then 204.

A reference is `{"secret": "<name>"}`. It is allowed in two places only:

- `tools[].auth_config.token`, as in the example above.
- `channels[].secret`, on a `whatsapp_evolution` channel (the Evolution API key). An `external` channel's signing secret is generated by parley and cannot be set; a `secret` on any other type is refused (step 8).

A plain string in either place is refused, and the error does not repeat it. Put no secret in `config` or `input_schema`: they are returned and exported as written.

- An apply reads the secrets it names at that moment. After `PUT /secrets/{name}` replaces a value, the next apply updates the tool or channel that uses it (`fields` shows `auth_config` or `secret`). Until then the tool keeps the old value.
- `DELETE /secrets/{name}` is refused with 409 while a managed tool or channel uses the secret, for example `secret is used by tool 'get_order'`; every user is listed by its manifest key (`secret is used by tool 'get_order', channel 'inbox'`). Take the reference out of the file and apply first. Replacing the value with `PUT /secrets/{name}` is always allowed.

Check: `GET /secrets` lists `orders_token` and has no `value`.

## 4. Dry run

`POST /manifest/apply` with `dry_run=true` returns what an apply would do and changes nothing. It needs the same secrets, and it refuses what an apply would refuse (steps 6 and 8).

```
curl -s -X POST "$BASE_URL/manifest/apply?dry_run=true" \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/yaml" \
  --data-binary @manifest.yaml
```

`Content-Type` is required: `application/json`, `application/yaml` or `text/yaml` (another value is 415). `dry_run` is `false` unless set.

Expected: 200 with `dry_run`, `changes` and `generated`. On an empty tenant:

```
{"dry_run": true,
 "changes": [
   {"kind": "department", "key": "billing", "action": "create", "fields": [], "id": null},
   {"kind": "department", "key": "shipping", "action": "create", "fields": [], "id": null},
   {"kind": "tool", "key": "get_order", "action": "create", "fields": [], "id": null},
   {"kind": "agent", "key": "support", "action": "create", "fields": [], "id": null},
   {"kind": "channel", "key": "inbox", "action": "create", "fields": [], "id": null}],
 "generated": {}}
```

Each change has `kind` (`department`, `tool`, `agent`, `channel`, `knowledge_entry` or `knowledge_qa`), `key` (the `key` of an agent or channel, the `name` of a department or tool, the `title` of an entry, the `question` of a pair), `action`, `fields` and `id`. `id` is the id of the item (the channel id that a message needs, for one). It is `null` for a `create` in a dry run, since nothing exists yet, and for a `delete` after it ran; a real apply gives the id of every created item, and a dry run gives the existing id for `update`, `adopt`, `unchanged` and `delete`. `changes` lists departments, knowledge entries and pairs, tools, agents and channels in that order, then the deletes (channels, agents, tools, departments, entries, pairs).

| `action` | Meaning | `fields` |
|---|---|---|
| `create` | No such item. | `[]` |
| `update` | A managed item differs from the file. | The names that differ, in the order of the create call of that item. |
| `adopt` | A hand-made item matches (step 7). It becomes managed. | The names that differ. |
| `unchanged` | Nothing to do. | `[]` |
| `delete` | A managed item is no longer in the file (step 6). | `[]` |

The names in `fields` are those of the item: for an agent, its `POST /agents` fields (not `key`); for a tool, its `POST /tools` fields; for a department, `name` or `description`; for a channel, `name`, `agent`, `config` or `secret`; for an entry, `title` or `text`; for a pair, `question` or `answer`. An `external` channel's signing secret is never compared.

Check: the call returns 200 and `GET /agents` lists nothing yet.

## 5. Apply

```
APPLIED=$(curl -s -X POST $BASE_URL/manifest/apply \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/yaml" \
  --data-binary @manifest.yaml | jq .)
EVENTS_SECRET=$(echo "$APPLIED" | jq -r .generated.channels.inbox.events_secret)
```

Expected: 200 with `dry_run` false and the same `changes` as the dry run, now done, except that every created item now has its `id`. The answer is 200 for a create too.

- `generated.channels.<key>.events_secret` is the signing secret of a new `external` channel, shown only in the response of the apply that creates it and never again. `$EVENTS_SECRET` is that value: store it ([project-channel](project-channel.md) step 2 says what it signs). If the response is lost, the secret is lost: there is no way to read or rotate it.
- `generated` is `{}` on every later apply, and on a dry run.
- Two applies of one tenant do not run at the same time; the second waits. If a conflicting change by another request lands in between, the answer is 409 `a conflicting change was made during the apply; apply again`.

Apply the same file again:

```
curl -s -X POST $BASE_URL/manifest/apply \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/yaml" \
  --data-binary @manifest.yaml
```

Expected: 200, every `action` is `unchanged`, `generated` is `{}`, and nothing is written.

Check: `curl -s $BASE_URL/agents -H "X-API-Key: $TENANT_KEY"` lists the agent `Acme support`, and `curl -s $BASE_URL/channels -H "X-API-Key: $TENANT_KEY"` lists `Project inbox`.

## 6. Change the file and apply again

Edit the file and send it again. In your project this is the same file; here the edited one is `manifest-v2.yaml`, so both stay on disk. It changes the persona, removes the `billing` department and adds `returns`:

```yaml
# file: manifest-v2.yaml
version: 1

agents:
  - key: support
    name: Acme support
    model: gpt-4o-mini
    temperature: 0.3
    instructions: |
      You are the support agent of Acme, an online shop. Answer briefly and
      kindly. When the customer gives an order number, look it up with
      get_order.
    tool_names:
      - get_order

departments:
  - name: shipping
    description: Where an order is, delivery dates and address changes
  - name: returns
    description: Returns, exchanges and damaged items

tools:
  - name: get_order
    description: Look up an order by id
    method: GET
    endpoint: https://api.example.com/orders
    input_schema:
      type: object
      properties:
        order_id:
          type: string
      required:
        - order_id
    auth_config:
      type: bearer
      token:
        secret: orders_token

channels:
  - key: inbox
    type: external
    name: Project inbox
    agent: support
    config:
      events_url: https://inbox.example.com/parley/events
```

```
curl -s -X POST "$BASE_URL/manifest/apply?dry_run=true" \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/yaml" \
  --data-binary @manifest-v2.yaml
curl -s -X POST $BASE_URL/manifest/apply \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/yaml" \
  --data-binary @manifest-v2.yaml
```

Expected: 200. The changes that are not `unchanged` (each also has its `id`, left out here):

```
{"kind": "department", "key": "returns", "action": "create", "fields": []}
{"kind": "agent", "key": "support", "action": "update", "fields": ["instructions", "temperature"]}
{"kind": "department", "key": "billing", "action": "delete", "fields": []}
```

Check: `curl -s $BASE_URL/categories -H "X-API-Key: $TENANT_KEY"` lists `shipping` and `returns` and not `billing`.

What a removal deletes, and when the apply refuses it. Only a managed item is ever deleted; an item made by hand is not.

| Removed from the file | Refused when |
|---|---|
| a channel | it has any conversation, or any contact |
| an agent | it has any conversation (open or closed), or a channel that stays still uses it |
| a tool | an agent that stays (in the file, or made by hand) lists it in `tool_names` |
| a department | an open conversation (not `CLOSED`) is filed under it. Closed conversations lose the department and are kept |

A refusal is 422 with a problem at the field `channels`, `agents`, `tools` or `departments`, naming the item and the count. The whole apply writes nothing, and the dry run refuses too. The conversation below makes the channel and its agent undeletable:

```
PROJECT_CHANNEL_ID=$(echo "$APPLIED" | jq -r '.changes[] | select(.kind == "channel" and .key == "inbox") | .id')
curl -s -X POST $BASE_URL/channels/external/messages \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d "{\"channel_id\": \"$PROJECT_CHANNEL_ID\", \"user_id\": \"ana\", \"message\": \"Where is my order?\"}"
curl -s -X POST "$BASE_URL/manifest/apply?dry_run=true" \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"version": 1}'
```

Expected: 201, then 422 with these `details`:

```
{"field": "channels", "message": "channel 'inbox' has 1 conversation; deleting it would erase it"}
{"field": "agents", "message": "agent 'support' has 1 conversation; deleting it would erase it"}
```

The other refusals read `department 'returns' has 1 open conversation; close them or move them to another department first` and `tool 'get_order' is used by agent 'support'; remove it from tool_names first`.

A channel with no conversation but with a contact: `channel 'inbox' has 1 contact; deleting it would erase it and its memories`. A channel that stays and names an agent that goes: `agent 'support' is the agent of channel 'Project inbox'; delete the channel or bind it to another agent first`.

Applying a file with only `version: 1` removes every managed item, so check the dry run first.

If someone deletes a managed item by hand, the next apply creates it again when the file still has it, and forgets it when the file does not.

## 7. Take over a tenant set up by hand

`GET /manifest` writes the tenant as a manifest: every agent, department, tool, channel, knowledge entry and pair, managed or not.

```
curl -s "$BASE_URL/manifest?format=yaml" -H "X-API-Key: $TENANT_KEY"
curl -s $BASE_URL/manifest -H "X-API-Key: $TENANT_KEY" | jq '{secrets_needed, warnings}'
```

Expected: 200. For the tenant of the steps above, `secrets_needed` is `[]` (`orders_token` is set) and `warnings` is `[]`. `format` is `json` (default) or `yaml`; any other value is 422. JSON is `{"manifest": {...}, "secrets_needed": [...], "warnings": [...]}`. YAML (`application/yaml`) is the manifest alone, preceded by the comment line `# secrets_needed: a, b` (or `none`) and one `# warning: ...` line per warning. Save the YAML and it is a file to apply.

- A managed item keeps its `key`. A hand-made agent or channel gets a key made from its name (lower case, other characters to `-`, at most 64, `-2` on a clash).
- A field equal to its default is left out, and so are empty sections and an empty `config`.
- A stored token becomes a reference named `tool_<tool name>_token` (the tool name slugged to `[a-z0-9_]`). A WhatsApp channel's key becomes `channel_<key>_secret`. The values are not exported and cannot be read. `secrets_needed` lists the referenced names not set yet: set each one with `PUT /secrets/{name}` and the real value before you apply. A managed item keeps the secret names it was applied with: the export of the file in step 2 names `orders_token`, so applying the export changes nothing. The generated names are only for items made by hand.
- An `external` channel's signing secret is not exported, and an apply never changes it.
- `warnings` says what the file cannot say: a document (`document '<title>' is not in the manifest: documents are uploaded through the API`), two hand-made agents with one name, a tool whose credentials cannot be decrypted, a managed department or tool renamed by hand.

Applying a file adopts the items already there instead of making copies. A hand-made item is matched only among items the manifest does not manage:

- departments and tools by `name`;
- agents by `name`;
- channels by `name` and `type`.

An adopted item keeps its id, so conversations stay attached. An adoption has the action `adopt`, and `fields` lists what the file changes: a field left out is set to its default, so read the dry run before the first apply. An adopted `external` channel keeps its signing secret, and `generated` stays empty.

Limits of adoption:

- Two hand-made items of one name (or one name and type) are refused: `2 agents are named 'Sales'; rename one or delete it`. Two items of the file that match one hand-made item are refused: `another agent in the file also adopts the agent named 'Sales'; rename one`.
- From the first apply the item is managed. Taking it out of the file deletes it, with the refusals of step 6. Items you leave out of the first file stay hand-made and untouched.

## 8. Errors

All validation errors are 422 in the API's error shape, and every problem is returned at once. `details` holds `{"field", "message"}`. A request with a body that is not valid gets:

```
curl -s -X POST "$BASE_URL/manifest/apply?dry_run=true" \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"version": 1, "agents": [{"key": "Support", "name": "x", "colour": "blue"}], "departments": [{"description": "x"}]}'
```

Expected: 422:

```
{"error": {"code": "validation_error", "message": "Validation failed", "request_id": "...",
  "details": [
    {"field": "agents[0].instructions", "message": "Field required"},
    {"field": "agents[0].model", "message": "Field required"},
    {"field": "agents[0].key", "message": "String should match pattern '^[a-z0-9_-]{1,64}$'"},
    {"field": "agents[0].colour", "message": "Extra inputs are not permitted"},
    {"field": "departments[0].name", "message": "Field required"}]}}
```

- `field` is where the problem is, the same path string as in the file: `agents[0].key`, `tools[0].auth_config` (a plain-text token is refused at the whole `auth_config`; a secret that is not set is reported at `tools[0].auth_config.token`), `(root)` for the document. A message names items and secrets and never carries a field value.
- The file is checked in two stages. Problems of shape (type, bounds, missing or unknown keys, `version`) come first, in no fixed order. Only a file with none of them gets the checks that need the tenant, and these come together in the second answer: duplicates, references, secrets, addresses, removals. Fix the first list, then expect a second one.
- `message` for a field is the API's own: the same message `POST /agents`, `POST /tools` and `POST /categories` give for that field. The `endpoint` and `events_url` messages are `invalid endpoint: ...` and `invalid events_url: ...` ([setup](setup.md) step 2, [project-channel](project-channel.md) step 2).
- The whole API uses this shape, so do `PUT /secrets/{name}` with a bad name and `GET /manifest` with a bad `format`.

Problems of shape:

| `field` | `message` |
|---|---|
| `version` | `Field required`; `Input should be 1` |
| any item | `Field required`; `Extra inputs are not permitted` |
| `tools[0].auth_config` or `channels[0].secret` | `credentials must be secret references; use {"secret": "<name>"}` |
| `agents[0].key` | `String should match pattern '^[a-z0-9_-]{1,64}$'` |
| a YAML value of the wrong kind | `Input should be a valid string (in YAML, quote the value)` (a bare `no`, `on` or `1.0` in a string field) |
| `agents` and the other sections | `List should have at most 50 items after validation, not 51` |
| `(root)` | `invalid JSON, line N: ...`, `invalid YAML, line N: ...`, `the body is not UTF-8`, `nested too deeply`, or `Input should be a valid dictionary or instance of Manifest` for a list or a number |

Problems found against the tenant:

| `field` | `message` |
|---|---|
| `tools[0].auth_config.token`, `channels[0].secret` | `secret 'orders_token' is not set` |
| `tools[0].endpoint` | `invalid endpoint: ...` |
| `channels[0].config` | `invalid events_url: ...` or `invalid base_url: ...` |
| `agents[0].tool_names[0]` | `unknown tool: not in the file, not a tool of the tenant and not a built-in` |
| `channels[0].agent` | `unknown agent: not an agent key in this file` |
| `channels[0].secret` | `a secret is only allowed on a whatsapp_evolution channel` |
| `channels[0].type` | `type cannot change; use a new key` |
| `agents[1].key`, `channels[1].key` | `duplicate key in the file` |
| `departments[1].name`, `tools[1].name` | `duplicate name in the file` |
| `agents[0].name`, `channels[0].name` | `2 agents are named 'Sales'; rename one or delete it` |
| `agents[0].name`, `channels[0].name` | `another agent in the file also adopts the agent named 'Sales'; rename one` |
| `departments[0].name`, `tools[0].name` | `already managed by the manifest under another name` and `already exists, not managed by the manifest` (both follow a hand rename of a managed department or tool) |
| `knowledge.entries[1].title`, `knowledge.qa[1].question` | `duplicate name in the file`, `already managed by the manifest under another name` and `already exists, not managed by the manifest` |
| `knowledge` | `knowledge limit exceeded: KNOWLEDGE_MAX_ITEMS is 500` (also `KNOWLEDGE_MAX_ITEM_CHARS` and `KNOWLEDGE_MAX_TOTAL_CHARS`, with the setting's value) |
| `channels`, `agents`, `tools`, `departments` | a refused removal (step 6); entries and pairs have no in-use rule |

Other statuses of `POST /manifest/apply`:

| Status | When |
|---|---|
| 401 | no `X-API-Key`, or a wrong one |
| 413 | the body is over 1 MiB (`request body too large`), counted as it arrives |
| 415 | `Content-Type` is not `application/json`, `application/yaml` or `text/yaml` |
| 409 | a concurrent change to a name (step 5) |

## 9. Facts that surprise

- The body is at most 1 MiB, JSON or YAML. Both go through the same checks. A list or a number at the top is 422.
- YAML is read as plain data. Anchors, aliases and merge keys are refused. Dates, binary values, keys that are not strings, `.inf`, `.nan` and nesting over 100 levels are refused at their path. Inside `config` and `input_schema` a bare `no` still loads as `false`: quote it when you mean text.
- A channel `type` cannot change: use a new `key` and remove the old channel (step 6 says when that is refused).
- Renaming is a create and a delete. A new `key` for an agent or a channel, or a new `name` for a department or a tool, makes a new item and deletes the old one; conversations do not move to it. An agent or a channel with conversations cannot be deleted, so keep its `key` and change its `name`. To rename a department with open conversations, apply a file with both names, move the conversations with `PUT /conversations/{conversation_id}/category`, then apply the file with the old name removed.
- An agent or a channel can be deleted only when it has no conversation at all; a department when none of its conversations is open (step 6).
- A hand edit of a managed item is overwritten by the next apply, field by field, as `update` lists it. A field left out of the file is reset, not kept.
- One tenant has one manifest. Applying another file removes the managed items the new file does not name. The examples of step 10 are meant for an empty tenant.
- The signing secret of an `external` channel is shown once (step 5).
- There is no section for documents or for importing tools. Any key that step 2 and the knowledge section do not list is refused. An export lists each document as a warning.

## 10. Examples

Three complete files, each valid and applied in the tests. Each needs the secret it names set first (step 3), and an empty tenant.

| File | Business | Secrets to set | Channel |
|---|---|---|---|
| `store.yaml` | An online shop. Persona in Portuguese. An order status tool, departments for sales, order tracking and support. | `store_orders_token` | `external`, with `events_url` |
| `clinic.yaml` | A clinic. Persona in Portuguese. Never gives a diagnosis. An appointment tool, one department per specialty and reception. | `clinic_agenda_key` | `webchat` |
| `law-firm.yaml` | A law firm. Persona in English. Never gives legal advice, escalates on sentiment, no tool. Departments by practice area. | none | `webchat` |

A running instance serves them without authentication at `GET /examples/manifests/{name}.yaml`, where `{name}` is `store`, `clinic` or `law-firm`. They are also in `examples/manifests/` of the repository.

```
curl -s $BASE_URL/examples/manifests/store.yaml -o store.yaml
curl -s -X PUT $BASE_URL/secrets/store_orders_token \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"value": "token-from-the-orders-system"}'
```

Expected: 200 with the file, then 204. Tool hosts are governed by `TOOL_ALLOWED_HOSTS` ([configuration](configuration.md)): with it empty, as in `.env.example`, the dry run below returns 200, because `api.example.com` is a public host; if you set it, list the hosts of the file in it or the dry run is 422 `invalid endpoint: host is not in TOOL_ALLOWED_HOSTS: ...`. Change the names, the personas, the URLs and the departments to your business, then apply with the call of step 5. The tool endpoints in them are documentation hosts (`https://api.example.com/...`) that pass the address check and answer nothing: point them at your own service before a customer writes.

Check: the dry run of step 4 with `--data-binary @store.yaml` returns 200 on an empty tenant and lists only `create`.

Next: connect the channel in [project-channel](project-channel.md) or [messaging](messaging.md), and tune the agent in [agent-behaviour](agent-behaviour.md).
