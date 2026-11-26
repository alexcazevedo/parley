# Brief 3: native providers for the agent turn

Mode: `mvp`. Routing profile `cloud`, sequential. Needs brief 2 done: the model layer it adopted is what this milestone extends to the reply.

Moved here from brief 2: typed answers for the auxiliary jobs on chat models. Brief 2 gave them only on TypeSafe's decision model; a job set to `llm:<model>` still answers in text.

## The problem

The reply can only run on an endpoint that speaks the OpenAI chat-completions format (`app/llm/factory.py`: "Only `openai_compatible` exists"). Models from Anthropic, Google and others are reachable only through a compatibility layer, which loses what their own APIs offer and breaks on small differences (the name of the token-limit field already needs a setting, `LLM_MAX_TOKENS_PARAM`). There is one endpoint and one key for the whole deployment, so two agents cannot use two vendors.

## What parley has today

- The agent has `provider` (only `openai_compatible` is accepted) and `model`.
- `LLMProvider.complete(messages, tools, model, temperature, max_tokens)` returns text or tool calls, plus usage.
- Messages carry text and images (`TextPart`, `ImagePart` in `app/llm/base.py`); `LLM_VISION_ENABLED` and `VISION_MAX_IMAGES` gate images.
- The loop in `app/agents/runtime.py` owns everything around the model call: the deadline, the ledger, terminal tools, the boundary check.
- After brief 2: the auxiliary jobs already run through the new model layer.

## Decided

**The reply moves onto the model layer** adopted in brief 2, still behind `LLMProvider`. Parley's loop does not change: one model call per step, tool calls executed by parley's registry and ledger, the reply checked before it is sent. The model layer is never given the tools to run by itself.

**Providers.** The agent's `provider` accepts the vendors the model layer supports natively, starting with OpenAI, Anthropic and Google, plus `openai_compatible` for everything else (local models, gateways). Each provider's credentials are set by the server owner in the environment. An agent naming a provider with no credentials is refused at creation and in the manifest with a message naming the missing setting.

**Nothing changes for an existing deployment.** `openai_compatible` with `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` keeps working, and stays the default.

**What must behave the same on every provider**, each proven by a test against the model layer's fake: tool calls and their results in history (including a history written under one provider and continued under another, since a tenant can change an agent's provider mid-conversation), images, the token limit, usage counts, timeouts and the turn deadline, and the error path that sends the agent's `error_message`.

**Retries.** One place retries a failed model call. Today `app/http_retry.py` does it for the compatible provider; the model layer and the vendors' own clients may retry too. The milestone sets one rule and turns the others off, so a turn never multiplies its wait.

**Recorded.** The agent run records the provider beside the model.

**Docs.** The configuration guide (credentials per provider), the setup and manifest guides (`provider` values), the agent-behaviour guide (what differs between providers that an integrator can notice), the operations guide (retries and timeouts).

## Open for the grill

1. Which providers ship in this milestone. Recommended: OpenAI, Anthropic, Google and `openai_compatible`; others when asked for.
2. Credentials per tenant (through the write-only tenant secrets the manifest already uses) or per server only. Recommended: per server only in this milestone; per tenant is a clean follow-up because the secrets store exists.
3. Speech-to-text (`STT_BASE_URL`, `STT_MODEL`) and embeddings (`EMBEDDINGS_BASE_URL`) stay on their OpenAI-compatible endpoints. Recommended: yes, unchanged.
4. Whether provider-specific features are exposed (prompt caching, extended reasoning). Recommended: prompt caching where the provider applies it without a request change; nothing that needs a new agent field.
5. Whether `LLM_MAX_TOKENS_PARAM` can be retired once the model layer handles the difference.

## Read before planning

- The current Pydantic AI documentation: each provider's model class and configuration, how to make a single model request with tool definitions without letting it execute tools, message history formats and whether a history can be replayed across providers, images, usage, retries and timeouts.
- In this repository: `app/llm/` (all of it, as brief 2 left it), `app/agents/runtime.py`, `app/agents/context.py`, `app/http_retry.py`, `app/api/agents.py`, `app/manifest/schema.py`, `app/config.py`, `docs/guides/configuration.md`, `docs/guides/messaging.md` (what the customer receives when the model fails).

## Tests

Each provider through the fake: a plain reply, a turn with a tool call, a terminal tool, an image, the token limit, a timeout, an error. A conversation continued after the agent's provider changed. An agent with a provider that has no credentials refused. The whole existing suite unchanged on `openai_compatible`.

## Out of scope

Streaming (brief 4). Per-tenant credentials unless the grill brings them in. Fallback from one provider to another on failure. Routing a message to a model by its difficulty. Provider-specific agent fields. Moving the agent loop into a framework.

## Done when

On one stack with credentials for two vendors, two agents of one tenant, each on a different provider, answer a customer; each run records its provider and model; a turn with a tool call and a turn that hands off work on both; a conversation started on one provider continues correctly after the agent is switched to the other; an agent created with a provider that has no credentials is refused with a message naming the setting; and a deployment configured only with `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` passes every existing test unchanged.
