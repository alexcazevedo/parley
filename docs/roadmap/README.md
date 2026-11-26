# Roadmap: tools and models

Four milestones, in the order to build them. Each file is a brief for one `/comb:auto` run in this repository: the decisions are in it, and what is still open is listed under "Open for the grill". The admin screen named by earlier milestones is separate and not in this list.

| Order | Brief | What it gives | Depends on |
|---|---|---|---|
| 1 | [Business tools over MCP](1-business-tools-mcp.md) | An app runs an MCP server; the agent calls its tools for the customer who is writing. **Done**: [guide](../guides/business-tools.md). The signature was left as a follow-up ([FOLLOW_UPS](../FOLLOW_UPS.md)) | nothing |
| 2 | [A model per job](2-model-per-job.md) | Each auxiliary decision (boundary check, sentiment, department) can run on a small, cheap model, with typed answers. **Done** for the decision model (TypeSafe); typed answers on chat models moved to brief 3: [configuration](../guides/configuration.md), [agent behaviour](../guides/agent-behaviour.md), [operations](../guides/operations.md) | nothing; easier after 1 |
| 3 | [Native providers](3-native-providers.md) | The agent turn can run on OpenAI, Anthropic and Google through their own APIs. **Done**: [configuration](../guides/configuration.md), [agent behaviour](../guides/agent-behaviour.md), [operations](../guides/operations.md); "others" and the follow-ups are in [FOLLOW_UPS](../FOLLOW_UPS.md) | 2 |
| 4 | [Streaming](4-streaming.md) | Web chat shows the reply as it is written | 3 |

## Decisions that hold across all four

- **Parley keeps its own agent loop.** The loop in `app/agents/runtime.py` runs under a deadline, records every tool call in the ledger before it runs, ends on a terminal tool, and checks the reply against the boundaries before it is sent. No milestone here hands that loop to a framework.
- **No LangChain and no LangGraph.** Parley already has what LangGraph would bring (loop, saved state, handoff to a person, retries), built around its own rules.
- **Pydantic AI enters in milestone 2, as the model layer only**, behind the existing `LLMProvider` interface (`app/llm/base.py`). It is the same stack as parley (Pydantic, async) and brings native providers and typed outputs. Milestone 1 does not need it.
- **MCP for tools, with parley as the client.** MCP defines discovery, the call and the result. Parley adds only what MCP leaves open: which customer the call is for, a call id for safe repeats, and (still to come, see FOLLOW_UPS) a signature.
- **HTTP tools stay.** `POST /tools` and the manifest's `tools` section keep working unchanged.
- **Nothing changes for an existing tenant until it opts in.** Every new capability is off by default.
- **No real model, embeddings or network call in tests.**

## Before starting any of them

The working tree must be clean and the previous milestone's tickets archived: a run that finds uncommitted work or unarchived tickets stops and says so instead of overwriting `PRODUCT.md`.

Library facts in these briefs (the MCP specification, the MCP Python SDK, Pydantic AI) were written from memory and may be out of date. Each run reads the current documentation before planning; each brief says what to check.

## One thing still to decide by the owner

Decided: the small decision model of milestone 2 is TypeSafe's (Jev), used through Pydantic AI's own support for it (`pydantic-ai-slim[typesafe]`, key in `TYPESAFE_API_KEY`). The live part of that milestone's success demo needs a TypeSafe key; without one the run proves it with a fake and says so.

## Prompts

Run one at a time, each after the previous is committed and its tickets archived.

```
/comb:auto mvp "Implement docs/roadmap/1-business-tools-mcp.md. That file is the brief: its decisions are made, do not re-ask them; grill only what it lists under 'Open for the grill'. Read docs/roadmap/README.md first for the decisions that hold across milestones. If the working tree has uncommitted work or tickets that are not archived, stop and say so instead of overwriting PRODUCT.md."
```

```
/comb:auto mvp "Implement docs/roadmap/2-model-per-job.md. That file is the brief: its decisions are made, do not re-ask them; grill only what it lists under 'Open for the grill'. Read docs/roadmap/README.md first for the decisions that hold across milestones. If the working tree has uncommitted work or tickets that are not archived, stop and say so instead of overwriting PRODUCT.md."
```

```
/comb:auto mvp "Implement docs/roadmap/3-native-providers.md. That file is the brief: its decisions are made, do not re-ask them; grill only what it lists under 'Open for the grill'. Read docs/roadmap/README.md first for the decisions that hold across milestones. If the working tree has uncommitted work or tickets that are not archived, stop and say so instead of overwriting PRODUCT.md."
```

```
/comb:auto mvp "Implement docs/roadmap/4-streaming.md. That file is the brief: its decisions are made, do not re-ask them; grill only what it lists under 'Open for the grill'. Read docs/roadmap/README.md first for the decisions that hold across milestones. If the working tree has uncommitted work or tickets that are not archived, stop and say so instead of overwriting PRODUCT.md."
```
