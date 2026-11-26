# REPORT: Native providers (milestone 6)

- **Vision:** implement `docs/roadmap/3-native-providers.md`: an agent's replies can run on OpenAI, Anthropic or Google through their own APIs, and the auxiliary decisions get typed answers on chat models.
- **Mode:** mvp. **Owner:** agent.
- **Outcome:** done. 5 tickets finished, each on its first attempt; `make check` is green (1495 passed, 1 skipped). Committed, pushed and merged to `main` for deployment at your request.
- **The main caveat:** no real vendor was called. No OpenAI, Anthropic or Google key was in `.env`, so everything here is proven with fakes only.

## Grill

AGREE 8, DIVERGE 0, ESCALATE 0. No DIVERGE or ESCALATE rows.

Decided: ship `openai`, `anthropic`, `google` beside `openai_compatible`; credentials per server; speech-to-text and embeddings unchanged; no provider-specific features or caching work; `LLM_MAX_TOKENS_PARAM` stays; a job the owner sets to a model gets typed answers, an unset job keeps the text form.

One answer settles a conflict inside the brief. "The reply moves onto the model layer" clashed with "nothing changes for an existing deployment" and your "every existing test passes unchanged". The owner chose: `openai_compatible`, the default and the only provider in production, stays on the existing hand-written provider, untouched. Only the three new values go through Pydantic AI. Retiring the old provider is a follow-up.

Plan verdict from the owner: APPROVE, no objections.

## Tickets

| # | ticket | model | status | attempts |
|---|---|---|---|---|
| 01 | Replies on `openai`, `anthropic`, `google` through Pydantic AI | opus | done | 1 of 3 |
| 02 | Refuse an agent whose provider is unknown or has no key; run records provider and model | sonnet | done | 1 of 3 |
| 03 | Typed answers for decisions on chat models; stub learns the form | opus | done | 1 of 3 |
| 04 | Success demo test; live script | sonnet | done; live script made no call (no key) | 1 of 3 |
| 05 | Docs | sonnet | done | 1 of 3 |

No blocked tickets.

## What did not go as planned

- **The live check did not run.** `scripts/demo_native_providers.py` exited 2 with `no provider key is set: live check not done`. The vision allowed for this.
- **Ticket 03 changed tests from the previous milestone**, as planned and approved: a decision job set to `llm:<model>` now answers in typed form, so three tests in `tests/test_model_jobs.py` and seven in `tests/test_model_compare.py` were updated. Every test that sets no job setting is untouched.
- **Agent runs had no model column.** Ticket 02 added both `provider` and `model`.
- I changed the script's default Claude model to `claude-haiku-5-5`; the OpenAI and Google defaults (`gpt-4o-mini`, `gemini-2.5-flash`) were not checked.

## How to run the result

```
uv run python scripts/demo_native_providers.py
uv run pytest -q tests/test_native_demo.py
```

With `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or `GOOGLE_API_KEY` set, the script asks each of those vendors for a plain reply, a tool call and a typed boundary decision, and prints latency and token use; it ends with `native providers demo: ok`. To use a provider: set its key on the server, then set the agent's `provider` and `model`.

## Final review: diff summary

40 files changed, 2853 insertions(+), 333 deletions(-), against `1613c8c`.

**New behaviour**
- An agent's `provider` accepts `openai`, `anthropic`, `google` beside `openai_compatible` (the default). Keys: `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, per server.
- Saving an agent with an unknown provider, or one whose key is not set, is refused on create, on update and in the manifest, naming the setting.
- On a native provider the reply is one model request per step through Pydantic AI. Parley still runs the tools, the ledger, the handoff and the boundary check. Parley's stored history is converted on every call, so a conversation continues after the agent's provider is changed.
- Retries on a native provider are done by the vendor's client, up to `HTTP_RETRY_ATTEMPTS`; parley adds none.
- Each agent run records `provider` and `model`.
- Job settings accept `llm:`, `openai:`, `anthropic:`, `google:` and `typesafe:`. On a chat form the three decisions return a typed answer; an answer that cannot be validated after one retry blocks the reply (boundary check). A vendor form without its key stops startup.
- The stub model answers the structured form.

**Where the code is**
- New: `app/llm/providers/native.py`, `scripts/demo_native_providers.py`.
- Changed: `app/llm/factory.py`, `app/llm/jobs.py`, `app/llm/decisions.py`, `app/llm/compare.py`, `app/api/agents.py`, `app/manifest/apply.py`, `app/models/agent_run.py`, `app/agents/runtime.py`, `app/agents/sentiment.py`, `app/agents/categorize.py`, `app/conversations/summary.py`, `app/handoff/summarizer.py`, `app/config.py`, `scripts/mock_llm_server.py`.
- Migration `0030`. Dependencies: `pydantic-ai-slim[anthropic,google,openai,typesafe]` 2.75.0, which brings the `openai`, `anthropic` and `google-genai` packages.
- Docs: configuration, setup, manifest, agent-behaviour (new section 9), operations, messaging, architecture, follow-ups, the roadmap.
- Tests: 4 new test files, about 105 tests.

**Decisions to check**
1. **Do not put a production agent on a new provider before running the live script with real keys.** See "Not verified".
2. **The production reply path is unchanged by design.** `openai_compatible` does not touch the new module; a test asserts it.
3. **`llm:<model>` changed meaning for the three decisions**, from text to typed. No deployment sets it today.
4. **Pydantic AI is now imported in two modules**, `app/llm/decisions.py` and `app/llm/providers/native.py`.
5. **Keys are per server.** Every tenant on the server shares a vendor key, and its cost.
6. **An agent already stored with a provider that is no longer served is left as it is**; its turns fail until the key is back or the agent is changed.
7. **`docs/ARCHITECTURE.md` is exactly at its 930-line limit.**

**Not verified**
- Any real call to OpenAI, Anthropic or Google.
- Which image formats each vendor accepts through this path.
- How each vendor reports token use (cached or reasoning tokens may be counted differently).
- Tool call ids across a provider switch on real vendors: the fake accepts any id.
- Whether OpenAI's strict mode accepts the pick-one schemas used for the rule and department picks.
- The typed form on a real OpenAI-compatible endpoint other than the stub.
- CI was not run. Production runs `main`, which has none of this.
