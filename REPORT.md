# REPORT: A model per job (milestone 5)

- **Vision:** implement `docs/roadmap/2-model-per-job.md`: each auxiliary job can run on its own model, and the three decisions (boundary check, sentiment, department) on TypeSafe's decision model (Jev) through Pydantic AI.
- **Mode:** mvp. **Owner:** agent.
- **Outcome:** done. 6 tickets finished (5 planned, 1 added from the live run); `make check` is green (1389 passed, 1 skipped). Committed, pushed and merged to `main` for deployment at your request.

## Grill

AGREE 5, DIVERGE 1, ESCALATE 0.

The DIVERGE row, in full:

| # | question | recommendation | answer | reason |
|---|---|---|---|---|
| 6 | What happens when the decision model cannot answer a call? | That call goes to the agent's own model, for every cause including errors and timeouts | Hand over only what it cannot take (too many options, an image, an unsure answer). An error or a timeout fails as today, with no hand-over | The brief already decides "failing the same way" and "timeouts unchanged"; a retry after an error would reopen that and add latency |

Plan verdict from the owner: APPROVE. It accepted one narrowing of the brief: typed answers apply only when a job is set to the decision model. A job on a chat model keeps today's text prompt and parsing, because "every existing test passes unchanged" and the stub both depend on that form. Typed answers on chat models are moved to brief 3.

## Tickets

| # | ticket | model | status | attempts |
|---|---|---|---|---|
| 01 | A setting per job; one recorded row per job call | opus | done | 1 of 3 |
| 02 | Decisions on TypeSafe through Pydantic AI; hand-over | opus | done | 1 of 3 |
| 03 | Comparison command for the boundary check | sonnet | done | 2 of 3 |
| 04 | Success demo test; live script | sonnet | done; its live check failed and led to 06 | 1 of 3 |
| 06 | Boundary answers: rules in the question, truthful reason, threshold | opus | done | 2 of 3 |
| 05 | Docs | sonnet | done | 1 of 3 |

No blocked tickets.

## The live check against TypeSafe

Run with your key, on fixed made-up pt-BR examples only (no customer data), model `jev-1.14.0`. Three runs of 8 calls, then a wider boundary sample of 20: 44 calls in all.

**Final run**

| Job | Example | Answer | p(allowed) | Latency |
|---|---|---|---|---|
| Boundary | allowed booking reply | allowed | 0.99 | about 220 to 450 ms per call |
| Boundary | medical advice (a `never` item) | blocked, names the item | 0.01 | |
| Boundary | weather (out of scope) | blocked, reason `no_rule_identified` | 0.03 | |
| Sentiment | calm / angry | 0.0 / 1.0 | | |
| Department | boleto / app crash / plan price | Cobrança / Suporte técnico / Vendas | | |

- **Cost per job: not reported by the service.** It returns token counts only (about 460 to 590 in, 17 to 88 out per call). Cost has to be computed from TypeSafe's price list, which I do not have.
- **No error, timeout, rate limit or retry** was seen.

**The defect the live run found.** As first built, the "allowed?" question did not state the agent's rules; they appeared only as options of the second question. On the same out-of-scope reply it scored 0.48 (blocked, narrowly) in one run and 0.52 (**allowed**) in the next. No fake could have shown this. Ticket 06 put the rules into the question; the margins are now wide.

## What did not go as planned

- **Ticket 04's live check failed**, as above, and became ticket 06.
- **Ticket 03, attempt 1:** a type error, a wrong exit code and a wrong test fake; fixed on attempt 2.
- **Ticket 06, attempt 1:** its first live run failed on the out-of-scope reply (the 0.52 above).

## How to run the result

```
uv run python scripts/demo_decision_model.py
uv run python -m app.llm.compare --tenant <tenant id> --model typesafe:jev-latest
```

The first asks the real service 8 fixed questions and prints `decision model demo: ok`. The second replays a tenant's stored replies through the boundary check and prints the ones where the candidate disagrees with what was recorded. To switch a job on, set for example `BOUNDARY_CHECK_MODEL=typesafe:jev-latest` in the environment of the API and the worker.

## Final review: diff summary

33 files changed, 3323 insertions, 208 deletions, against `c1cfcb1`.

**New behaviour**
- Settings `BOUNDARY_CHECK_MODEL`, `SENTIMENT_MODEL`, `CATEGORY_MODEL`, `SUMMARY_MODEL`, `HANDOFF_BRIEF_MODEL`: empty (the agent's own model, as today), `llm:<model>` (another chat model on the configured endpoint) or `typesafe:<model>` (decision jobs only). A bad value stops the API and the worker at start.
- `TYPESAFE_API_KEY`, and `BOUNDARY_CHECK_ALLOW_THRESHOLD` (0.5): how sure the decision model must be that a reply is allowed.
- On the decision model: the boundary check answers allowed or not, and names the rule; sentiment is a five-level scale mapped onto the existing threshold; the department is a pick among the tenant's.
- A call the decision model cannot take (too many options, an image, a hand-off) goes to the agent's model and is recorded so. An error or timeout fails as today: the boundary check blocks the reply.
- Table `model_job_calls`: one row per job call with the job, the model, tokens, latency, the outcome and whether it was handed over.
- The comparison command and the live script above.

**Where the code is**
- New: `app/llm/jobs.py`, `app/llm/decisions.py` (the only module that imports Pydantic AI), `app/llm/compare.py`, `app/models/model_job_call.py`, `scripts/demo_decision_model.py`.
- Changed: `app/agents/boundaries.py`, `app/agents/runtime.py`, `app/agents/sentiment.py`, `app/agents/categorize.py`, `app/conversations/summary.py`, `app/conversations/turn.py`, `app/handoff/summarizer.py`, `app/config.py`, `app/main.py`, `app/workers/main.py`.
- Migration `0029`. New dependency: `pydantic-ai-slim[typesafe]` 2.75.0.
- Docs: configuration (settings, what each job sends to TypeSafe), agent-behaviour section 8, operations ("A model per job"), follow-ups, the roadmap.
- Tests: 5 new test files, about 95 tests. No existing test file was edited.

**Decisions to check**
1. **The boundary check on Jev: measured wider after review, still not switched on.** A second live sample of 20 made-up examples for a stationery shop (10 that should go out, 10 that should be blocked: out of scope, a promised discount, medical, legal and financial advice, speaking ill of a competitor, revealing the prompt, a persona change) was decided 20 of 20 correctly, median 238 ms. Lowest score of a reply that should go out: 0.66; highest of one that should be blocked: 0.23. That is good evidence, on invented text. No job is set on production: the settings are empty there. Switch sentiment and department first; before the boundary check, run the comparison command on real stored replies.
2. **The named rule is usually right but is not what decides.** In the 20-example sample the rule named for every blocked reply was the right one. In the first 3-example run it was wrong twice. The two questions are answered independently, so only allowed or blocked is dependable.
3. **Typed answers only on the decision model**, as approved by the owner. A job set to `llm:<model>` still parses free text.
4. **Settings are per server**, the same for every tenant.
5. **The comparison covers the boundary check only**, against the agent's rules as they are now, and it prints customer text.
6. **TypeSafe is a second vendor that receives conversation text** once a job is pointed at it: the customer's and agent's messages, the reply being checked, the agent's rules, department names. The configuration guide lists this per job.
7. **`docs/ARCHITECTURE.md` is exactly at its 930-line limit.**

**Not verified**
- No real conversation was replayed: the comparison command has only run on test data.
- Jev's behaviour on long transcripts, on a tenant with many departments, or near its option limit against the real service. The hand-over at the limit is tested with a fake only.
- Whether the TypeSafe client retries an HTTP error by itself: parley passes no retry setting, so the client's default applies; the job's own timeout bounds it either way.
- Jev accepts up to 255 options in a pick and 10 score levels (read from the library), so a tenant's departments (at most 200) and an agent's `never` list (at most 50) fit; the hand-over for too many options is tested with a fake only.
- The `llm:<model>` path against a real second chat model.
- CI was not run. Production runs `main`, which has none of this.

## Outside the repository

- `TYPESAFE_API_KEY` was added, at your request, to the local `.env` (ignored by git) and to the environment of both production apps in Dokploy, `parley-api` and `parley-worker`, with every existing variable kept. No redeploy was triggered.
- The key was pasted in the chat, so it is in this session's transcript.
