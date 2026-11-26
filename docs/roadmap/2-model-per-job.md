# Brief 2: a model per job

Mode: `mvp`. Routing profile `cloud`, sequential.

## The problem

A turn can make several model calls besides the one that writes the reply. Most of them are decisions, not writing, and all of them run on the agent's own model, which is the most expensive and slowest choice for a yes-or-no answer. Their answers are also parsed out of free text, so a model that adds a word breaks them.

## What parley has today

One provider, `openai_compatible` (`app/llm/factory.py`), behind one interface, `LLMProvider.complete` (`app/llm/base.py`). One deployment-wide endpoint and key (`LLM_BASE_URL`, `LLM_API_KEY`), and the model named on the agent (`LLM_MODEL` when blank).

The calls a turn can make:

| Job | Where | What it returns today | Kind |
|---|---|---|---|
| Reply | `app/agents/runtime.py` | text or tool calls | writing |
| Boundary check | `app/agents/boundaries.py` (`check_reply`) | `ALLOW` or a reason, parsed from text | decision; it is the safety gate and fails closed |
| Sentiment | `app/agents/sentiment.py` (`score_frustration`) | a number, parsed from text | decision |
| Department | `app/agents/categorize.py` (`categorize_conversation`) | an index, parsed from text | decision |
| Rolling summary | `app/conversations/summary.py` | text | writing |
| Handoff brief | `app/handoff/summarizer.py` | text | writing |

All six use the agent's provider and model. The summary reads `agent.model` directly, the others go through `resolve_model`.

## Decided

**A model per job.** Each of the five auxiliary jobs can be given its own provider and model. The default for every job is the agent's own model, so nothing changes until someone opts in.

**Where it is set.** By the server owner in the environment, one optional setting per job, the same for every tenant: the same pattern as every other limit and endpoint in parley. A small model is an operating cost decision, and the owner of the server pays for the model.

**Typed answers.** The three decision jobs ask for a structured result instead of free text: the boundary check an allow flag and a reason, sentiment a number in a range, the department a choice among the listed ones or none. The "unparseable answer" cases shrink to what the model layer rejects.

**The model layer.** Pydantic AI is adopted here, as the model layer only, behind the existing `LLMProvider` interface. Parley keeps its own loop, ledger, handoff and boundary check. This milestone uses it for the auxiliary jobs; the reply stays on today's provider until brief 3. Reasons: it is the same stack as parley, it has typed outputs, and it reaches many providers, which brief 3 needs. Rejected: LangChain or LangGraph (a runtime parley does not need), hand-written structured output per provider.

**Failing the same way.** Every job keeps its present behaviour on failure and timeout. The boundary check fails closed (the reply is blocked) on an error, a timeout or an answer it cannot use, with the same reasons recorded on the agent run (`check_failed`, `check_unparseable`). Sentiment and department keep their timeouts (`SENTIMENT_TIMEOUT_SECONDS`, `CATEGORY_TIMEOUT_SECONDS`) and do nothing on failure.

**Measured before trusted.** A command replays recorded conversations of a tenant through a job on two models and prints where they disagree: for the boundary check, the replies one blocked and the other allowed. Parley already stores runs, blocked replies and reasons. No model is swapped by default by this milestone; the command is how an owner decides.

**Recorded.** Each agent run records which model served each job and its token use, so the saving can be read from the data.

**The stub and the tests.** The stub model answers the structured form of each job; `tests/test_mock_llm_server.py` keeps failing when a prompt changes and the stub no longer recognises it. Tests use the model layer's own fake or scripted model; no network.

**Docs.** The configuration guide (every setting), the agent-behaviour guide (which jobs exist, what each decides, what a smaller model risks for each), the operations guide (the comparison command, how to read the recorded use).

## Open for the grill

1. The decision model itself. The owner named "Jev"; it could not be identified. With no named model, the milestone ships the mechanism and documents how to point a job at any model the layer can reach. If the owner names one, check that the model layer supports its vendor and its structured output.
2. Whether a job can also be set per agent (through the API and the manifest), or only per server in this milestone. Recommended: per server only.
3. Whether the summary and the handoff brief get a setting too (they write text, so a small model may write worse ones). Recommended: yes, same mechanism, with the risk stated in the guide.
4. Whether to add a pre-check job, "is a model needed at all for this message", for cases such as a bare greeting or "ok". Recommended: no, a later milestone, after there is data from the recorded use.

## Read before planning

- The current Pydantic AI documentation: supported providers, how a model is named and configured, structured output and what it does when a provider has no native support for it, timeouts, the test models, how usage is reported.
- In this repository: `app/llm/` (all of it), `app/agents/boundaries.py`, `app/agents/sentiment.py`, `app/agents/categorize.py`, `app/conversations/summary.py`, `app/handoff/summarizer.py`, `app/agents/runtime.py` (`_enforce_boundaries`), `app/models/agent_run.py`, `app/config.py`, `scripts/mock_llm_server.py`, `tests/test_mock_llm_server.py`, `docs/guides/agent-behaviour.md`, `docs/guides/configuration.md`.

## Tests

Each job on its own model and on the default; the structured answer of each decision job; every failure path of the boundary check still blocking; timeouts unchanged; the comparison command on stored runs; the recorded model and use per job; an existing deployment with no new setting behaving exactly as before.

## Out of scope

Moving the reply to another provider (brief 3). Streaming (brief 4). Per-tenant model settings. Training or fine-tuning a model. Replacing a model call by rules or by a classical classifier. Changing what any job decides or its thresholds. Any agent framework owning the loop.

## Done when

On a stack where the reply runs on one model and the boundary check, sentiment and department each run on a second, smaller one set in the environment, a customer message is answered; the agent run shows which model served each job and its use; a reply that breaks a boundary is still blocked, and is blocked when the small model errors or times out; the comparison command, run on that tenant's stored conversations, prints the boundary decisions where the two models disagree; and with none of the new settings present, every existing test passes unchanged.
