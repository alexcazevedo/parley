# STATE — rewrite after every ticket completes; never append. ≤30 lines total.

## Now
operator-ui mvp run COMPLETE 2026-08-25 — tickets 24–25 done (1 attempt each); suite 163 green; live HTTP demo passes (handoff → inbox with brief → operator reply → resume → AI answers; Assumir moves ACTIVE_AI → HUMAN_HANDOFF). Browser visual check NOT done (Chrome extension unavailable). Awaiting final diff review (mvp approval 2 of 2).

## Next
- user opens http://localhost:8001/operator for the visual check
- final diff review with user → commit + push

## Decisions
- execution: sequential in place (25 depends on 24)
- no new endpoints/auth: page drives existing REST; key gate via localStorage `harness_operator_api_key`
- list API additive: repeatable state=, newest-activity order, limit, contact label (one query), include_handoff via one DISTINCT ON query
- prior: barge-in cd29d41 (pushed), sentiment fd19d19, warm-handoff 6e05006, humanizer a7aa4c4, web chat 892a13f; host ports 5434/6380/8001/9000

## Blockers
- none
