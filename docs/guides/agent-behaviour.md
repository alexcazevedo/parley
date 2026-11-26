# Agent behaviour

Tune how the agent answers: its instructions, memory, boundaries, escalation, and the texts customers see. Start from the variables of [setup](setup.md): `$BASE_URL`, `$TENANT_KEY`, `$AGENT_ID`, and `$CONTACT_ID` where a step needs it. Every field is set at create (`POST /agents`) or changed later with `PATCH /agents/{agent_id}`. Defaults and bounds of each field are in the table in [setup](setup.md) step 3; this guide says what each one does.

Variable added here: `$MEMORY_ID`, captured with `jq` in the memory step.

Every request sends `X-API-Key: $TENANT_KEY`. A change applies from the agent's next turn, to existing conversations too.

`PATCH` changes only the fields you send. An explicit `null` clears `boundary_scope`, `boundary_refusal_message`, `error_message` and `unsupported_media_message` (back to the default text). For `boundary_never`, `boundary_check_enabled`, `sentiment_escalation_enabled` and `handoff_message`, `null` means leave as it is.

## 1. Instructions and model settings

```
curl -s -X PATCH $BASE_URL/agents/$AGENT_ID \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"instructions": "You are the support agent for Acme. Answer in the customer'"'"'s language, briefly. Ask for the order number before looking anything up.", "temperature": 0.3}'
```

Expected: 200 with the whole agent.

What the fields do:

- `instructions`: the start of the system prompt on every turn. Parley then appends, in this order: the contact's memories, the conversation summary, the knowledge section when `knowledge_enabled` is on, a chat-style hint when the channel has `humanize` on, and the boundaries.
- `provider`: only `openai_compatible` works. The model endpoint and key come from `LLM_BASE_URL` and `LLM_API_KEY`, one for the whole deployment.
- `model`: sent to the provider as given.
- `temperature` and `max_tokens`: sent on each model call of a turn. `max_tokens` is sent under the name in `LLM_MAX_TOKENS_PARAM`.
- `tool_names`: the tools of yours the model may call, plus the built-ins below.
- `mcp_servers` (default `[]`): names of your MCP servers (registered with `POST /mcp-servers`) whose imported tools the model may call; a name with no such server is ignored, as in `tool_names`. Each tool is offered as `<server>__<tool>` with the server's description and schema, including tools found by a later sync. A call carries the conversation, channel, user id, contact name and a call id in the request's `_meta` (`parley/...` keys), never in the arguments. A tool marked read-only may be called again when an earlier attempt's outcome is unknown; any other is not. A tool's error result reaches the model as a failed call it can explain; a successful result is given as reference data with a note never to follow commands inside it. What the server must offer, the `_meta` keys and how to make a write safe to repeat are in [business-tools](business-tools.md). To try one tool outside a conversation, `POST /mcp-servers/{server_id}/call` with `tool` (its name on the server), `arguments` and `user_id` returns `ok`, `result` and `error`, and writes no tool execution record.
- `max_tool_iterations`: the most model calls in one turn. If the model is still calling tools after that many, the customer gets the fixed text `Desculpe, não consegui concluir. Vou encaminhar.` Despite the wording, the conversation is not handed to a human.
- `memory_enabled` and `handoff_enabled`: give the model the built-in tools `remember` and `handoff_to_human`. `memory_enabled` also turns memory reading on.
- `knowledge_enabled` (default `false`): gives the model the built-in tool `search_knowledge` with one parameter, `query`, which returns the matching passages of the tenant's knowledge base. The system prompt then lists the titles of up to 30 ready items (never their content) and tells the model to search before answering about the business, to answer only from what the search returns, and to say it does not know or hand off when nothing matches. Passages are given to the model as reference text, with a note never to follow commands inside them; the boundary check still judges the reply. The passages a call returned are kept in the run's tool execution record (`tool_executions`, see [knowledge](knowledge.md) step 9).

Check: `curl -s $BASE_URL/agents/$AGENT_ID -H "X-API-Key: $TENANT_KEY"` returns the new `instructions` and `temperature`.

## 2. Memory

Long-term memory is facts about one contact, kept across conversations with that contact on that channel.

- With `memory_enabled` `true` the model has the tool `remember` with `type` (`preference`, `fact`, `relationship`, `previous_issue`, `summary` or `custom`), `content`, and optional `importance` (1 to 5, default 3). The model decides when to call it; mention it in `instructions` if you want it used.
- Storing the same text again (ignoring case and extra spaces) does not add a row; it only raises `importance` when the new value is higher.
- Each turn the agent reads the contact's memories that have not expired, most important first, then newest, up to `MEMORY_CONTEXT_LIMIT` (default 20, 1 to 100). Only their `content` goes into the prompt, as a list under "Known facts about this contact". It also goes into the operator brief at handoff.
- With `memory_enabled` `false` the agent neither writes nor reads memories. The API below still stores them.

The memories API, per contact (`$CONTACT_ID` from [setup](setup.md) step 5, or the `contact_id` of a conversation):

- `POST /contacts/{contact_id}/memories`: body `type` (the six values above), `content`, `importance` (1 to 5, default 3), `expires_at` (optional time), `metadata` (optional object). Expected: 201 with `id`, `contact_id`, `type`, `content`, `importance`, `metadata`, `last_accessed_at`, `expires_at`, `created_at`. The same dedupe applies: a repeat returns the existing memory, still 201.
- `GET /contacts/{contact_id}/memories`: `query` (text contained in `content`, any case) and `limit` (default 20, 1 to 100). Not paged. Expired memories are not listed.
- `DELETE /contacts/{contact_id}/memories/{memory_id}`: 204. 404 `memory not found` or `contact not found`.

```
MEMORY_ID=$(curl -s -X POST $BASE_URL/contacts/$CONTACT_ID/memories \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"type": "preference", "content": "Prefers answers in Portuguese", "importance": 4}' | jq -r .id)
```

`$MEMORY_ID` is the `id`.

Check: `curl -s "$BASE_URL/contacts/$CONTACT_ID/memories?query=portuguese" -H "X-API-Key: $TENANT_KEY"` lists it. `DELETE` it with `$MEMORY_ID` and the same request returns `[]`.

## 3. Boundaries

Boundaries keep the agent on topic. Three fields describe them, and one switch turns the reply check on or off:

| Field | Meaning |
|---|---|
| `boundary_scope` | What the agent may help with, free text, up to 2000 characters. |
| `boundary_never` | Things it must never do or say, up to 50 items of 1 to 300 characters (spaces trimmed). |
| `boundary_refusal_message` | What the customer receives when a reply is blocked. Default `Desculpe, não posso ajudar com esse assunto.` |
| `boundary_check_enabled` | `true` (default): check each reply. `false`: no check, the prompt rule only. |

```
curl -s -X PATCH $BASE_URL/agents/$AGENT_ID \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"boundary_scope": "Acme orders, shipping and returns", "boundary_never": ["give legal advice", "promise refunds"], "boundary_refusal_message": "I can only help with Acme orders."}'
```

What happens:

1. With a scope or a never item set, a `## Boundaries` section is added to the system prompt: only help with the scope, never do the listed things, stay in role and never reveal the instructions.
2. With `boundary_check_enabled` `true`, every reply the model writes is judged by one more model call (same `model`, unless `BOUNDARY_CHECK_MODEL` says otherwise, section 8) before it is sent. It answers allow or block. A block replaces the reply with `boundary_refusal_message`.
3. The check fails closed: if the call errors, passes `BOUNDARY_CHECK_TIMEOUT_SECONDS` (default 8), or answers in an unexpected form, the reply is blocked. A slow model therefore shows as refusals; raise the timeout.
4. Only replies the model writes are checked. The handoff, failure, unsupported-media and refusal texts are sent as they are. The blocked reply is kept on the agent run and is not shown to the customer.
5. With no scope and no never items there is nothing to enforce: no prompt section and no check call.

The deployment floor: `BOUNDARY_FLOOR_FILE` points to a JSON file `{"scope": "text", "never": ["text"]}` that applies to every agent of every tenant. Its scope and never items are added to the agent's own, and tenants cannot remove them. While a floor file has content, the reply check is always on, whatever `boundary_check_enabled` says. A missing or invalid file stops the process at start. See [configuration](configuration.md).

More about the checks:

- The check is one extra model call per model-written reply, counted in the run's `llm_calls`. A blocked reply is refused at once; the model is not asked again.
- The agent run keeps `boundary_reason` (`check_failed` and `check_unparseable` for the fail-closed cases) and `blocked_text` (the original reply, never sent), and a `boundary.reply_blocked` warning is logged. No endpoint shows them; read `agent_runs` and the logs.
- The floor file takes the keys `scope` (non-empty string) and `never` (list of non-empty strings) and nothing else. It is read once at start-up by both `api` and `worker`, so both must be able to read it and an edit applies after a restart. An empty `BOUNDARY_FLOOR_FILE` means no floor.

Check: after the change above, a message such as "write me a poem about cats" gets the refusal text (the model must judge it out of scope; this is not guaranteed). `GET /agents/{agent_id}` returns the fields you set.

## 4. Sentiment escalation

Set `sentiment_escalation_enabled` to `true` and the agent hands a frustrated customer to a human.

```
curl -s -X PATCH $BASE_URL/agents/$AGENT_ID \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"sentiment_escalation_enabled": true}'
```

- Before the agent answers a batch of messages (while the conversation is `ACTIVE_AI`), one model call scores the customer's frustration from 0.0 (calm) to 1.0 (furious), looking at the last 10 messages and the new ones.
- A score at or above `SENTIMENT_THRESHOLD` (default 0.7) hands the conversation over. A lower threshold escalates sooner. Below it, the turn goes on normally.
- The handoff is recorded with `requested_by` `system` and reason `frustração do usuário detectada (score 0.xx)`. The customer is sent the agent's `handoff_message`. The agent does not answer that batch.
- The check is best effort. If the call errors, passes `SENTIMENT_TIMEOUT_SECONDS` (default 5), or returns no number, there is no escalation and the turn goes on.
- It adds one model call to every turn of an agent with it on.

Check: `GET /conversations/{conversation_id}` after an escalation shows `state` `HUMAN_HANDOFF` and `latest_handoff.requested_by` `system`.

## 5. The handoff message

`handoff_message` is the text the customer receives when the agent or the sentiment check hands the conversation over (1 to 1000 characters). Created without it, the agent stores `Vou te transferir para um atendente humano.`; send another text to change it.

- The agent's own handoff (tool `handoff_to_human`, with a `reason` argument) ends the turn: the customer receives `handoff_message`, not text the model wrote. It needs `handoff_enabled` `true`. Tell the model in `instructions` when to use it.
- An operator's `POST /conversations/{conversation_id}/handoff` sends nothing to the customer. See [operators](operators.md).
- If sending the handoff message fails, the handoff stands and the next attempt sends only that message.

```
curl -s -X PATCH $BASE_URL/agents/$AGENT_ID \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"handoff_message": "One moment, I am passing you to a colleague."}'
```

Check: the response has the new `handoff_message`.

## 6. Failure texts

Three texts are the agent's to set, each 1 to 1000 characters. `null` brings back the default. The defaults are Portuguese; set your customers' language.

| Field | Sent when | Default |
|---|---|---|
| `error_message` | The agent run fails (model error, timeout, an error in the loop), or a turn fails every retry. | `Desculpe, tive um problema técnico. Pode tentar novamente?` |
| `unsupported_media_message` | The whole batch is media the agent cannot read (video, document, sticker, location, contact, or media that failed to download). | `Por enquanto só consigo entender texto, áudios e imagens. Pode me mandar em texto?` |
| `boundary_refusal_message` | A reply is blocked (step 3). | `Desculpe, não posso ajudar com esse assunto.` |

Two more texts are fixed and cannot be changed: `Desculpe, não consegui concluir. Vou encaminhar.` (step 1) and `Desculpe, não entendi. Pode reformular?` (the model returned an empty reply).

The full list of what a customer receives, with the retry timing, is in [messaging](messaging.md).

```
curl -s -X PATCH $BASE_URL/agents/$AGENT_ID \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d '{"error_message": "Sorry, something went wrong on our side. Please try again.", "unsupported_media_message": "I can only read text, voice notes and images."}'
```

Check: the response shows both texts. `PATCH` with `{"error_message": null}` returns `error_message` as `null`, and the default is used again.

## 7. Context window and summary

- `context_message_limit` (default 20, 1 to 200): how many of the most recent messages (customer, agent and operator messages) the model sees each turn. Tool calls and results belonging to those messages come along, so a call is never separated from its result and an agent that uses many tools sends a longer prompt than the limit suggests; there is no separate cap on its size. Older messages are not sent.
- `summary_threshold` (default 40, 0 to 1000): after a reply, if at least this many messages have piled up since the last summary, parley makes one more model call that folds them into the conversation's `summary`. The summary goes into the system prompt on later turns. `0` turns summaries off. A failed summary never costs the customer a reply.
- The summary call uses the agent's `model` with fixed settings (temperature 0.2, up to 1024 tokens), unless `SUMMARY_MODEL` names another chat model (section 8).
- Keep the summary threshold at or above the window so the summary describes messages that left the window. Both costs grow with the numbers: the window is sent on every turn, the summary is one extra call each time the threshold is reached.

Check: after enough messages, `GET /conversations/{conversation_id}` returns a non-null `summary`. With `summary_threshold` `0` it stays `null`.

## 8. A model for each job

A turn can make five calls besides the reply. Each runs on the agent's own `model` unless the server owner sets it to another (settings and values in [configuration](configuration.md); the owner sets them in the environment, for every tenant, not per agent). The five:

| Job | Setting | What it does |
|---|---|---|
| Boundary check | `BOUNDARY_CHECK_MODEL` | Decides whether a reply keeps the rules (section 3). |
| Sentiment | `SENTIMENT_MODEL` | Scores the customer's frustration (section 4). |
| Category | `CATEGORY_MODEL` | Files the conversation under one of the tenant's departments, or none. |
| Summary | `SUMMARY_MODEL` | Writes the rolling summary (section 7). |
| Handoff brief | `HANDOFF_BRIEF_MODEL` | Writes the brief the operator reads at a handoff ([operators](operators.md)). |

The first three decide, so they can run on `typesafe:<model>`, TypeSafe's decision model, which answers typed questions instead of writing text. The summary and the brief write text and take only `llm:<model>` or nothing. Each job call is recorded in `model_job_calls` ([operations](operations.md)). The reply itself always runs on the agent's model.

How a decision model's typed answer becomes the job's result:

- Boundary check: two questions, answered independently. `allowed` decides: the reply goes out only when the model's probability of "allowed" reaches `BOUNDARY_CHECK_ALLOW_THRESHOLD` (default 0.5, from 0 to 1; higher is stricter). `broken` names the rule: the scope, a `never` item, staying in role, or none. A blocked reply records that rule as `boundary_reason` (the text of the `never` item, or `scope`, or `role`). A block with no rule named records `no_rule_identified`. An allowed reply goes out even if a rule was picked beside it, with no reason. The two answers can disagree, so the reason is a hint, not proof. The check's question carries the agent's rules; the answer to `allowed` is judged against them.
- Sentiment: a level from 0 to 4 against a rubric (calm, slightly annoyed, impatient, an explicit complaint or insults, furious), mapped to 0.0, 0.25, 0.5, 0.75 and 1.0. It meets `SENTIMENT_THRESHOLD` (default 0.7) as before: levels 3 and 4 escalate, at the default.
- Category: a pick of one department or "none". None changes nothing.

When the decision model cannot take a call, that call goes to the agent's own model, with the job's usual prompt, and the row in `model_job_calls` has `handed_over` true and names the agent's model. This happens when the options are more than the decision model picks from (a tenant with many departments or `never` items), when the evidence holds an image (sentiment and category), and when the model is unsure. An error or a timeout does not hand over. The job's own timeout (`BOUNDARY_CHECK_TIMEOUT_SECONDS`, `SENTIMENT_TIMEOUT_SECONDS`, `CATEGORY_TIMEOUT_SECONDS`) covers the decision and a hand-over together.

What fails closed: the boundary check. An error, a timeout or an answer that cannot be read blocks the reply (`check_failed`, `check_unparseable`), on every model. Sentiment and category do nothing on a failure: no escalation, no new category. A failed summary or brief is skipped as before.

What a smaller model risks:

- Boundary check: a wrongly allowed reply, an out-of-scope or rule-breaking reply that goes to the customer, and more wrong blocks of good replies. A strict threshold trades the first for the second. Compare before you trust it ([operations](operations.md)).
- Sentiment: a missed escalation, or an escalation of a customer who is only blunt.
- Category: a wrong or no department.
- Summary and brief: worse text. A summary that loses a fact is sent to the agent on later turns, and a weak brief costs the operator time. Nothing blocks a poor one.

Check: set one job in the environment, restart `api` and `worker`, send a message, and read the new row in `model_job_calls`.

Next: errors, paging and health in [api-conventions](api-conventions.md).
