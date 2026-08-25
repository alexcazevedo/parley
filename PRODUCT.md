---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: parallel     # sequential | parallel
---

# Conversational Agent Harness

## What / why

Headless, multi-tenant infrastructure for AI-powered conversational apps. The
harness owns the operational complexity of a conversation — ingestion, queue,
per-conversation debounce, conversation state, short/long-term memory, LLM
execution, tool orchestration, human handoff, retries, idempotency,
observability — so a customer only implements business logic as HTTP tools.
Principle: **the customer implements the business tools; the harness manages
the conversation and agent runtime.** Modular monolith: Python 3.12, FastAPI,
PostgreSQL (SQLAlchemy 2 + Alembic), Redis (queue + locks + debounce),
Docker Compose (api, worker, postgres, redis).

Two real channels behind one `ChannelAdapter` interface:

- **WhatsApp via Evolution API** — webhook in, Evolution REST send out,
  per-channel webhook secret. Credentials from `.env`; nothing invented.
- **Web Chat** — REST only (`POST /channels/webchat/messages`, `GET` poll for
  replies). Customer builds their own widget. The dev simulator
  (`POST /dev/messages`) uses this same path; it never bypasses the pipeline.

One `OpenAICompatibleProvider` (configurable `base_url` + `api_key`) behind an
`LLMProvider` abstraction covers OpenAI, OpenRouter, Ollama and local servers.
Live demo runs against an OpenAI key; tests use a fake provider.

Auth: per-tenant API key (`X-API-Key`) scopes every management/operator call;
one admin key for `/tenants`. All rows carry `tenant_id`; repositories are
shaped so PostgreSQL RLS can be added later.

Memory + handoff are written by the agent through two harness-provided
built-in tools — `remember(type, content, importance)` and
`handoff_to_human(reason)` — plus REST CRUD on `/contacts/{id}/memories` for
customers. No background extraction job. Long-term retrieval is structured
SQL; interface allows semantic retrieval later.

## Primary user story

As a developer integrating the harness, I register a tenant, an agent
(instructions + model + tool list), and HTTP tools describing my business
capabilities, then point a WhatsApp (Evolution) instance or my web widget at
the harness — so end users have multi-turn conversations where the agent
calls my tools, remembers facts about the contact, and can be handed to a
human operator, without me writing any session/queue/memory/retry code.

## Out of scope

- Telegram, Slack, Instagram, SMS adapters (interface ready, not built)
- WebSocket/SSE streaming, embeddable widget — REST poll only
- Native Anthropic/Gemini providers (only OpenAI-compatible)
- Multi-agent orchestration — one agent per conversation
- Visual workflow builder, workflow engine, event sourcing
- Vector/semantic memory (pgvector optional hook, not required)
- Automatic post-turn memory extraction
- Full operator dashboard — REST endpoints only
- Kafka, Kubernetes, microservices
- Voice, audio/image/document payloads (text only; models leave room)
- Database-per-tenant, JWT/OAuth
- Advanced RAG, fine-tuning, marketplaces

## Success demo

Done when: a user sends "Oi" / "Queria saber onde está" / "meu pedido 12345"
in quick succession (via `/dev/messages` and via a mocked Evolution webhook),
the harness persists all three, debounces them into one agent turn, resolves
tenant/contact/conversation, builds short-term context + long-term memories,
the agent calls a mocked `get_order` HTTP tool with `order_id="12345"`, the
final answer is persisted and sent through the channel adapter — then the
user says "Quero falar com uma pessoa", the agent calls `handoff_to_human`,
state flips `ACTIVE_AI → HUMAN_HANDOFF`, AI stops replying while new messages
still persist, an operator posts via `POST /conversations/{id}/messages`, and
`POST /conversations/{id}/resume` brings the AI back — all covered by `pytest`
(tenant isolation, idempotency, debounce, context, memory, tool success/fail,
max iterations, handoff/resume) and a scripted run against `docker compose up`.
