---
id: 02
status: doing
model: sonnet
depends_on: [01]
max_attempts: 3
files:
  - migrations/versions/0002_core.py
  - app/models/agent.py
  - app/models/channel.py
  - app/models/tool.py
  - app/models/contact.py
  - app/models/conversation.py
  - app/models/message.py
  - app/models/memory.py
  - app/models/tool_execution.py
  - app/models/agent_run.py
  - app/models/handoff.py
  - app/models/__init__.py
  - app/repositories/__init__.py
  - app/repositories/base.py
  - app/repositories/agents.py
  - app/repositories/channels.py
  - app/repositories/tools.py
  - app/repositories/contacts.py
  - app/repositories/conversations.py
  - app/repositories/messages.py
  - app/security/secrets.py
  - app/api/agents.py
  - app/api/channels.py
  - app/api/tools.py
  - app/api/contacts.py
  - app/api/conversations.py
  - app/main.py
  - tests/test_tenant_isolation.py
  - tests/test_crud.py
---
## Goal
Full tenant-scoped data model + migration + repositories + CRUD REST for agents, channels, tools, contacts; read-only list/get for conversations.

## Context
All tables use `TenantScoped`. Enums as `str` enums.

- `Agent(name, instructions text, provider='openai_compatible', model, temperature, max_tokens=1024, max_tool_iterations=5, memory_enabled bool, handoff_enabled bool, tool_names json[], context_message_limit=20, summary_threshold=40)`
- `Channel(type enum webchat|whatsapp_evolution, name, agent_id FK, config json, secret_encrypted text)`
- `Tool(name, description, input_schema json, endpoint, method, auth_config_encrypted text, timeout_seconds=10)`; unique `(tenant_id, name)`
- `Contact(channel_id, external_user_id, display_name, attributes json)`; unique `(tenant_id, channel_id, external_user_id)`
- `Conversation(contact_id, channel_id, agent_id, state enum ACTIVE_AI|WAITING|HUMAN_HANDOFF|CLOSED, summary text, last_message_at)`
- `Message(conversation_id, direction in|out, role user|assistant|human_operator|tool|system, type text, content, external_message_id, raw_payload json, agent_run_id nullable, tool_call_id nullable)`; unique `(tenant_id, channel_id, external_message_id)` where not null
- `Memory(contact_id, type enum preference|fact|relationship|previous_issue|summary|custom, content, importance int 1-5, last_accessed_at, expires_at, metadata json)`
- `AgentRun(conversation_id, status, input_text, output_text, llm_calls int, tool_calls int, prompt_tokens, completion_tokens, latency_ms, error)`
- `ToolExecution(agent_run_id, tool_id, tool_name, arguments json, result json, status ok|error, latency_ms, error)`
- `Handoff(conversation_id, reason, requested_by agent|user|operator|system, resolved_at)`

`security/secrets.py`: Fernet encrypt/decrypt with `SECRETS_KEY`. Repos take `tenant_id` explicitly in every method — no query without it.

## Checks (run in order — cheapest first)
1. `uv run ruff check .`
2. `uv run alembic upgrade head`
3. `uv run pytest -q`

## Done when
- Tenant A creates agent/tool; tenant B `GET /agents`, `GET /tools/{id}` sees nothing / 404.
- Tool `auth_config` never returned in API responses; stored encrypted.

## Not included
- Message ingestion, queue, workers, conversation mutation endpoints.
