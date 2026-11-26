---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
owner: agent                   # human | agent (set by comb:auto)
---

# Native providers (milestone 6)

The brief is `docs/roadmap/3-native-providers.md`; the decisions common to
the roadmap are in `docs/roadmap/README.md`. This file is the short form.

## What / why

A reply can only run on an endpoint that speaks the OpenAI chat-completions
format, with one endpoint and one key for the whole server. Models from
Anthropic and Google are reachable only through a compatibility layer, and
two agents cannot use two vendors. This milestone lets an agent run on
OpenAI, Anthropic or Google through their own APIs, and gives typed answers
to the auxiliary decisions when they run on a chat model.

## Primary user story

As the owner of a parley server, I put one key per vendor in the
environment; a tenant then sets an agent's `provider` and `model`, and that
agent's replies, tool calls and handoffs work as on any other, while every
agent I do not touch keeps running exactly as before.

## In scope

**Providers.** An agent's `provider` accepts `openai`, `anthropic` and
`google` beside `openai_compatible`, which stays the default. Credentials
are per server: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`. An
agent naming a provider with no key, or an unknown provider, is refused at
creation, on update and in the manifest, with a message naming the setting.

**The reply on a native provider** goes through Pydantic AI as one model
request per step. Parley's loop is unchanged: it executes the tool calls
through its registry and ledger, ends on a terminal tool, and checks the
reply before it is sent. The model layer never runs a tool.

**The same on every provider**, proven with the library's fakes: a plain
reply, tool calls and their results in history, a history written under one
provider and continued under another, images, the token limit, usage, the
turn deadline, and the error path that sends the agent's `error_message`.

**The default path does not move.** `openai_compatible` stays on the
existing hand-written provider, with `LLM_BASE_URL`, `LLM_API_KEY`,
`LLM_MODEL` and `LLM_MAX_TOKENS_PARAM`.

**One retry rule.** A failed model call on a native provider is retried by
the vendor's client up to the existing `HTTP_RETRY_ATTEMPTS`; parley adds
none on top.

**Recorded.** The agent run records the provider beside the model.

**Typed answers on chat models.** A job the server owner sets to a model
(`llm:`, `openai:`, `anthropic:` or `google:` followed by the model) gets a
structured answer for the three decisions; the stub model learns that form.
A job left unset keeps today's text prompt and parsing on the agent's own
model. The writing jobs accept the same forms and still write text.

**Docs.** Configuration, setup, manifest, agent behaviour, operations.

## Out of scope

Moving `openai_compatible` onto Pydantic AI and deleting the hand-written
provider (a follow-up, after the new path has run in production). Streaming.
Credentials per tenant. Fallback from one provider to another. Routing by
difficulty. Provider-specific agent fields. Prompt caching and reasoning
settings. Speech-to-text and embeddings, which stay as they are. Other
vendors. Any framework owning the loop.

## Constraints

- With `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` as the only model
  settings, every existing test passes unchanged.
- No test calls a real model. A live check runs only for a provider whose
  key is in `.env`; none is on this machine.
- Pydantic AI is imported only in `app/llm/decisions.py` and the new native
  provider module.

## Success demo

Done when, on one stack with credentials for two vendors, two agents of one
tenant, each on a different provider, answer a customer; each run records
its provider and model; a turn with a tool call and a turn that hands off
work on both; a conversation started on one provider continues correctly
after the agent is switched to the other; an agent created with a provider
that has no credentials is refused with a message naming the setting; a
decision job set to a chat model returns a typed answer; and a deployment
with only `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL` passes every
existing test unchanged. The vendors are fakes in tests.

## Grill log

| # | question | recommendation | answer | tag |
|---|----------|----------------|--------|-----|
| 1 | Mode | (given by the human) | mvp | AGREE |
| 2 | Which providers ship? | `openai`, `anthropic`, `google`, plus `openai_compatible` | As recommended | AGREE |
| 3 | Speech-to-text and embeddings unchanged? | Yes | Yes | AGREE |
| 4 | Provider-specific features? | None that needs a field or setting; no caching work | As recommended; caching a vendor applies by itself stays on; request-level caching is a follow-up | AGREE |
| 5 | Credentials per server or per tenant? | Per server; per tenant is a follow-up | Per server | AGREE |
| 6 | What serves `openai_compatible`? | The existing hand-written provider, untouched; only the new values go through Pydantic AI | As recommended; retiring the old path is a follow-up | AGREE |
| 7 | Retire `LLM_MAX_TOKENS_PARAM`? | No; it belongs to the hand-written provider | It stays, for `openai_compatible` only | AGREE |
| 8 | Which jobs get typed answers on a chat model? | A job the owner sets to a model; an unset job keeps the text form | As recommended; writing jobs accept the same forms and write text | AGREE |
