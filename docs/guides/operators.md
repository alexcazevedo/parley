# Operators

Hand a conversation to a person, and build an operator screen on the API. Start from the variables of [setup](setup.md) and [messaging](messaging.md): `$BASE_URL`, `$TENANT_KEY`, `$AGENT_ID` and `$CONVERSATION_ID` (a conversation with at least one customer message). Errors, paging and polling are in [api-conventions](api-conventions.md).

Variables added here:

```
OPERATOR_ID=alice
LAST_SEQ=<highest seq seen so far>
```

`$CATEGORY_ID` is captured with `jq` in step 8. `$LAST_SEQ` is set as in [messaging](messaging.md) step 2.

Every request sends `X-API-Key: $TENANT_KEY`. Operators use the tenant key too: parley has no operator accounts.

## Conversation states

| State | Meaning |
|---|---|
| `ACTIVE_AI` | The agent answers. Every new conversation starts here. |
| `HUMAN_HANDOFF` | A human has it. The agent stays silent. Unclaimed ones are in the waiting lines. |
| `CLOSED` | Finished. The customer's next message opens a new conversation. |
| `WAITING` | Exists in the API but nothing sets it. Treat it as `ACTIVE_AI`. |

Who moves a conversation:

| Move | By | Allowed from |
|---|---|---|
| to `HUMAN_HANDOFF` | the agent, with its `handoff_to_human` tool (needs `handoff_enabled`); the sentiment check (needs `sentiment_escalation_enabled`); an operator, with `POST /conversations/{conversation_id}/handoff` | `ACTIVE_AI` (or `WAITING`) only. Otherwise 409 `cannot hand off a conversation in state <state>`. |
| to `ACTIVE_AI` | an operator, with `POST /conversations/{conversation_id}/resume` | `HUMAN_HANDOFF` only. Otherwise 409 `cannot resume a conversation in state <state>`. |
| to `CLOSED` | an operator, with `POST /conversations/{conversation_id}/close` | any state, including `CLOSED` (200 again). |

The customer and the agent can never resume or close. When the agent or the sentiment check hands over, the customer is sent the agent's `handoff_message`. When an operator hands over with the endpoint, the customer is sent nothing; send a message yourself (step 5) if you want one.

An operator's move wins over a running turn: once committed, the agent sends nothing more.

Each handoff stores a record: `reason`, `requested_by` (`agent`, `system` or `operator`), `created_at`, and a short `summary` for the operator that a model writes after the handoff. The summary is best effort and can be `null`.

## 1. Hand a conversation to a human

`POST /conversations/{conversation_id}/handoff`, body `{"reason": "..."}` (`reason` is optional text for the operator).

```
curl -s -X POST $BASE_URL/conversations/$CONVERSATION_ID/handoff \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"reason": "customer asked for a person"}'
```

Expected: 200 with the conversation: `state` `HUMAN_HANDOFF`, `waiting_since` set, `claimed_by` `null`. `latest_handoff` is `null` in this response. 404 `conversation not found`.

For a customer-side client the handoff text arrives as an ordinary `assistant` message (only when the agent or the sentiment check hands over; an operator's handoff sends none). The client detects the handoff by reading `state` from `GET /conversations/{conversation_id}`.

Check: `curl -s $BASE_URL/conversations/$CONVERSATION_ID -H "X-API-Key: $TENANT_KEY"` returns `state` `HUMAN_HANDOFF` and `latest_handoff` with `requested_by` `operator` and your `reason`. `summary` fills in a few seconds later, if it can be written.

## 2. Read the waiting lines

`GET /lines` returns the queue as a list of lines. Query: `limit` (default 200, 1 to 500, caps the total across lines, keeping the oldest) and `include_handoff` (default `false`; `true` fills `latest_handoff` on each conversation).

```
curl -s "$BASE_URL/lines?include_handoff=true" -H "X-API-Key: $TENANT_KEY"
```

Expected: 200 with one line per category of the tenant (by name, empty ones too), then a last line with `category_id` `null` for the uncategorized, always present. Each line has `category_id`, `category_name` and `conversations`. A conversation is in a line only while it is `HUMAN_HANDOFF` and unclaimed, oldest handoff first (`waiting_since`). The list is not paged.

Check: the conversation from step 1 appears in the last line (or in its category's line), with `waiting_since` set.

## 3. Claim it

`POST /conversations/{conversation_id}/claim`, body `{"operator_id": "..."}`: 1 to 200 characters, spaces trimmed. Parley does not check it; use your own operator's id.

```
curl -s -X POST $BASE_URL/conversations/$CONVERSATION_ID/claim \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d "{\"operator_id\": \"$OPERATOR_ID\"}"
```

Expected: 200 with `claimed_by` equal to the id and `claimed_at` set. The conversation leaves the lines. Claiming your own claim again is 200. Only one caller wins a race; the other gets 409 `conversation is already claimed`. A conversation that is not `HUMAN_HANDOFF` gives 409 `conversation is not waiting for an operator`.

Check: `GET /lines` no longer lists it, and `GET /conversations?state=HUMAN_HANDOFF&claimed_by=$OPERATOR_ID` does.

## 4. Read the conversation

`GET /conversations/{conversation_id}` returns the conversation and, always, `latest_handoff` (`reason`, `summary`, `requested_by`, `created_at`). Messages are read as in [messaging](messaging.md) step 2: `GET /conversations/{conversation_id}/messages`, first without `after_seq`, then with `after_seq=$LAST_SEQ`.

Conversation fields:

| Field | Meaning |
|---|---|
| `id`, `contact_id`, `channel_id`, `agent_id` | Identity. |
| `state` | See above. |
| `summary` | The agent's rolling summary of older messages, or `null`. |
| `last_message_at`, `created_at` | Times. |
| `contact_external_user_id`, `contact_display_name` | Who the customer is (the `user_id` or phone number, and the contact's name). |
| `category_id`, `category_locked` | The category, and whether a human chose it. |
| `waiting_since` | When it entered the waiting lines. `null` outside a handoff. |
| `claimed_by`, `claimed_at` | The claim, or `null`. |
| `latest_handoff` | Newest handoff record, or `null`. Filled only by `GET /conversations/{conversation_id}`, and by `GET /conversations` and `GET /lines` with `include_handoff=true`. Always `null` in the responses of handoff, claim, release, resume, close and `PUT .../category`: read the conversation again if you need it. |

Check: the response has `claimed_by` equal to `$OPERATOR_ID` and a `latest_handoff` with `reason`.

## 5. Reply as the operator

`POST /conversations/{conversation_id}/messages`, body `{"content": "..."}`: 1 to `MAX_MESSAGE_CHARS` characters (422 `validation_error` otherwise).

```
curl -s -X POST $BASE_URL/conversations/$CONVERSATION_ID/messages \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"content": "Hi, this is Alice. Let me look at your order."}'
```

Expected: 201 with the message: `role` `human_operator`, `direction` `out`. It is sent through the conversation's channel (on WhatsApp, to the customer's number), then stored. On web chat the customer sees it by polling.

Facts to design around:

- The API does not require a claim to reply, and does not check the state. Anyone with the tenant key can reply to any conversation, claimed by someone else or not, in `ACTIVE_AI` or `CLOSED` too. Replying does not claim, hand off or change the state; in `ACTIVE_AI` the agent keeps answering as well.
- The message does not record which operator wrote it. Keep that on your side if you need it.
- If the channel address is refused, the response is 502 `channel endpoint refused` and nothing is stored. Any other send failure (for example Evolution answering with an error) is a 500 and nothing is stored.

Check: `GET /conversations/{conversation_id}/messages?after_seq=$LAST_SEQ` shows the new message.

## 6. Release, resume or close

- Release: `POST /conversations/{conversation_id}/release`, no body. Clears the claim and keeps `waiting_since`, so the conversation returns to its place in the lines. 409 `conversation is not claimed` if nobody holds it.
- Resume: `POST /conversations/{conversation_id}/resume`, no body. State becomes `ACTIVE_AI`, the claim and `waiting_since` are cleared, the handoff record is closed. The customer is told nothing. The agent answers the customer's next message. Messages sent during the handoff got no agent reply.
- Close: `POST /conversations/{conversation_id}/close`, no body. State becomes `CLOSED`, claim and `waiting_since` cleared. The agent says nothing.

All three return 200 with the conversation. Like claim, they return `latest_handoff` as `null`; only the single read and the lists with `include_handoff=true` fill it (step 4).

```
curl -s -X POST $BASE_URL/conversations/$CONVERSATION_ID/resume -H "X-API-Key: $TENANT_KEY"
```

Check: the response has `state` `ACTIVE_AI`, `claimed_by` `null` and `waiting_since` `null`. Resuming it again gives 409 `cannot resume a conversation in state ACTIVE_AI`.

## 7. List and filter conversations

`GET /conversations` is the inbox: most recent activity first, paged ([api-conventions](api-conventions.md)).

| Query | Meaning |
|---|---|
| `state` | Repeat it for several: `state=HUMAN_HANDOFF&state=ACTIVE_AI`. Values: `ACTIVE_AI`, `WAITING`, `HUMAN_HANDOFF`, `CLOSED`. Absent: every state. |
| `category_id` | Only that category. |
| `uncategorized` | `true`: only conversations with no category. Together with `category_id`: 422 `category_id and uncategorized cannot be combined`. |
| `claimed_by` | Only conversations claimed by that operator id. |
| `include_handoff` | `true` fills `latest_handoff` on each row. |
| `limit`, `cursor` | Paging. |

```
curl -s "$BASE_URL/conversations?state=HUMAN_HANDOFF&claimed_by=$OPERATOR_ID&include_handoff=true&limit=100" -H "X-API-Key: $TENANT_KEY"
```

Check: the response is a list of the operator's open handoffs, newest activity first. There is no filter by customer; match `contact_external_user_id` yourself.

## 8. Categories

Categories are the tenant's subjects. Conversations are filed under one, and `GET /lines` has one line per category.

Define: `POST /categories` with `name` (1 to 80 characters, spaces trimmed, unique per tenant: 409 `category name already exists`) and optional `description`. Write the description for the model: it reads `name: description` for each category when filing.

```
CATEGORY_ID=$(curl -s -X POST $BASE_URL/categories \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"name": "billing", "description": "Charges, invoices, refunds and payment problems"}' | jq -r .id)
```

Expected: 201 with `id`, `name`, `description`, `created_at`. `$CATEGORY_ID` is the `id`. Also: `GET /categories` (paged, by name), `GET /categories/{category_id}`, `PATCH /categories/{category_id}` (`name`, `description`), `DELETE /categories/{category_id}` (204). A deleted category leaves its conversations with no category and their place in the lines. Moving a conversation to another category keeps `waiting_since`, so it joins the new line at its original place. 404 `category not found`.

How conversations get a category:

- On every batch of customer messages the model is asked to pick one category from the tenant's list, or none. This also runs while a conversation is in `HUMAN_HANDOFF`, without a reply. With no categories defined there is no model call.
- The model never creates a category and never clears one. "None fits", an unreadable answer or a timeout (`CATEGORY_TIMEOUT_SECONDS`) leaves the category as it was.
- A human's choice wins: `PUT /conversations/{conversation_id}/category` with `{"category_id": "<id>"}` or `{"category_id": null}` sets it and locks it (`category_locked` becomes `true`). The model never changes a locked conversation. Allowed in every state. Unknown category: 404 `category not found`. There is no call that unlocks; a new conversation starts unlocked.

```
curl -s -X PUT $BASE_URL/conversations/$CONVERSATION_ID/category \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d "{\"category_id\": \"$CATEGORY_ID\"}"
```

Check: the response has `category_id` equal to `$CATEGORY_ID` and `category_locked` `true`.

## 9. A minimal operator screen: the polling loop

The calls, in the order a screen makes them. The values are the ones the demo operator page uses.

1. On load, ask for the operator's id and keep it as `$OPERATOR_ID`.
2. Every 3 seconds, for the queue: `GET /lines?include_handoff=true`. Draw one block per line; show `contact_display_name` or `contact_external_user_id`, `latest_handoff.reason` and `latest_handoff.summary`, and the time since `waiting_since`. Redraw only when the response changed.
3. Every 3 seconds, for "mine": `GET /conversations?state=HUMAN_HANDOFF&claimed_by=$OPERATOR_ID&include_handoff=true&limit=100`.
4. When the operator takes a conversation: `POST /conversations/{conversation_id}/claim`. On 409 reload step 2 and tell the operator someone else has it.
5. When a conversation is open: load `GET /conversations/{conversation_id}/messages` once, set `$LAST_SEQ` to the highest `seq`, then every 1.5 seconds `GET /conversations/{conversation_id}/messages?after_seq=$LAST_SEQ`, appending what comes and updating `$LAST_SEQ`. Render `role` `user` on one side and `assistant` and `human_operator` on the other. Fetch media with `GET /media/{message_id}` and the key header; an `<img>` tag cannot send it, so fetch and show a blob.
6. To send: `POST /conversations/{conversation_id}/messages`. Do not insert the message yourself; the next poll returns it.
7. Buttons: `POST /conversations/{conversation_id}/release`, `POST /conversations/{conversation_id}/resume`, `POST /conversations/{conversation_id}/close`, and `PUT /conversations/{conversation_id}/category` for a category picker filled from `GET /categories`. Show the 409 `message` when a move is refused; another operator may have moved the conversation.
8. Disable the reply box unless `state` is `HUMAN_HANDOFF` and `claimed_by` is yours. This is your rule: the API does not enforce it.

Check: with two browser tabs on different operator ids, claiming from one makes the conversation disappear from the other's next `GET /lines`, and its claim attempt gets 409 `conversation is already claimed`.

Next: tune the agent in [agent-behaviour](agent-behaviour.md).
