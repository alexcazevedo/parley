# STATE — rewrite after every ticket completes; never append. ≤30 lines total.

## Now
barge-in mvp run COMPLETE 2026-08-25 — tickets 22–23 done (1 attempt each); suite 154 green; live probe passes (interrupt mid-LLM → run superseded then success, single reply about order 99, nothing answers twice). Awaiting final diff review (mvp approval 2 of 2).

## Next
- final diff review with user → commit

## Decisions
- execution: sequential in place (23 depends on 22)
- supersede signal = existing Redis pending list (`has_pending` after drain); no new transport
- checkpoints: before delivery + between humanizer bubbles only; escalation path exempt; probe failure → today's behavior
- turn API: `process_turn(..., *, supersede_check)` → `Message | Superseded(delivered_parts) | None`; scheduler requeues drained batch at list head (`debounce.requeue_pending`) then existing tail re-arms now
- AgentRunStatus.SUPERSEDED via migration 0007 (ALTER TYPE ADD VALUE, autocommit block, no-op downgrade)
- prior: sentiment fd19d19, warm-handoff 6e05006, humanizer a7aa4c4, web chat 892a13f; host ports 5434/6380/8001/9000

## Blockers
- none
