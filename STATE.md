# STATE — rewrite after every ticket completes; never append. ≤30 lines total.

## Now
sentiment-escalation mvp run COMPLETE 2026-08-25 — tickets 20–21 done (1 attempt each) + orchestrator calibration micro-fix; suite 142 green; live rage probe passes with ticket's exact wording (score 0.70 → HUMAN_HANDOFF, requested_by=system, brief, handoff_message delivered, no AI answer). Awaiting final diff review (mvp approval 2 of 2).

## Next
- final diff review with user → commit

## Decisions
- micro-fix post-21: classifier prompt gained rubric anchors (0.7–0.8 = explicit complaint about service/assistant, sarcasm, insults) — original prompt scored "que atendimento horrivel" 0.6, under 0.7 threshold; threshold kept at 0.7 (anchoring the prompt beats blunt threshold drop)
- turn gate: _maybe_escalate_on_sentiment pre-runtime; _deliver helper shared by normal + escalation paths; handoff before delivery (state correctness over latency)
- 'system' already in pg enum — migration 0006 = agents.sentiment_escalation_enabled only
- prior: warm-handoff 6e05006, humanizer a7aa4c4, web chat 892a13f; host ports 5434/6380/8001/9000

## Blockers
- none
