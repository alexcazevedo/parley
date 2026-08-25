---
id: 05
status: doing
model: opus
depends_on: [04]
max_attempts: 3
files:
  - app/llm/__init__.py
  - app/llm/base.py
  - app/llm/providers/__init__.py
  - app/llm/providers/openai_compatible.py
  - app/llm/providers/fake.py
  - app/llm/factory.py
  - app/agents/__init__.py
  - app/agents/context.py
  - app/agents/runtime.py
  - app/tools/__init__.py
  - app/tools/base.py
  - app/tools/registry.py
  - app/conversations/turn.py
  - app/workers/main.py
  - tests/test_context_builder.py
  - tests/test_agent_loop.py
---
## Goal
Real agent turn: ContextBuilder → LLM (OpenAI-compatible or fake) → tool-call loop with iteration cap → persisted AgentRun + assistant reply.

## Context
`llm/base.py`: `LLMMessage(role, content, tool_calls?, tool_call_id?)`, `ToolSpec(name, description, parameters)`, `LLMResponse(content, tool_calls: list[ToolCall(id,name,arguments dict)], usage, latency_ms)`, `LLMProvider.complete(messages, tools, model, temperature, max_tokens)`. `OpenAICompatibleProvider` via `httpx.AsyncClient` POST `{base_url}/chat/completions` (OpenAI tools format). `FakeLLMProvider(scripted: list[LLMResponse])` pops in order; test fixture.

`agents/context.py::ContextBuilder.build(agent, conversation, recent_messages, memories, tools)` → system prompt = `agent.instructions` + `"\n\n## Known facts about this contact\n- ..."` (if memories) + `"\n\n## Conversation summary\n..."` (if summary); then last `agent.context_message_limit` messages (user/assistant/tool roles mapped). Never include secrets/tool endpoints.

`tools/base.py`: `ToolExecutor` protocol `execute(name, arguments, ctx) -> ToolResult(ok, data|error)`; `tools/registry.py` merges executors (HTTP arrives in 06, builtins in 07) — ship `NoopExecutor` returning error "unknown tool".

`runtime.py::AgentRuntime.run`: create AgentRun; loop ≤ `max_tool_iterations`: complete → if tool_calls: execute each, persist Message role=tool, append; else finish. On cap: final text `agent.max_iterations_message` default "Desculpe, não consegui concluir. Vou encaminhar." Overall `asyncio.timeout(60)`. Record tokens/latency/counters on AgentRun. Wire into `turn.py` replacing Echo (memories = [] for now).

## Checks (run in order — cheapest first)
1. `uv run ruff check .`
2. `uv run pytest -q tests/test_context_builder.py tests/test_agent_loop.py`
3. `uv run pytest -q`

## Done when
- Fake LLM scripted `[tool_call get_order, final text]` → 2 LLM calls, tool Message persisted, assistant reply persisted + sent.
- Fake LLM returning tool_call forever → stops at `max_tool_iterations`, AgentRun.status=`max_iterations`.
- Context contains ≤ N messages, summary and memories when present.

## Not included
- HTTP tool execution, SSRF, memory writes, summaries, handoff.
