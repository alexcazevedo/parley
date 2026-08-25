---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
---

# Sentiment-triggered escalation

## What / why

Human agents hand a conversation off instinctively when a customer is
getting angry; today's harness only escalates when the user explicitly
asks (agent calls `handoff_to_human`) or an operator intervenes. Item #4
of the seamless roadmap: detect frustration and escalate proactively —
before the user has to demand a person.

Shape (decided during grilling):

- **Detection — harness classifier, not agent discretion**: a cheap
  separate LLM call runs BEFORE the agent turn, scoring the inbound
  debounced batch (with recent conversation context) for user frustration,
  0.0–1.0. Deterministic and auditable; works even when the agent model
  would stubbornly keep trying. Language-agnostic (LLM judges, no keyword
  lists).
- **Trigger behavior — skip the agent, de-escalate fast**: score ≥
  threshold on an `ACTIVE_AI` conversation short-circuits the turn: no
  normal AI answer; conversation transitions to `HUMAN_HANDOFF` through
  the existing warm-handoff path (#3 — operator brief generated, reason
  notes detected frustration + score); user receives the agent's existing
  `handoff_message` text as the reply, delivered through the normal
  channel path (humanizer pacing applies).
- **Attribution**: the handoff records that the SYSTEM (sentiment
  detector) requested it — visible in `latest_handoff.requested_by`
  (enum extension + migration).
- **Config**: per-agent enable flag (default off; demo agent on). Global
  Settings: threshold (default 0.7), classifier timeout. Classifier uses
  the existing LLM provider.
- **Safety**: classifier is strictly best-effort — failure or timeout logs
  and the normal agent turn proceeds. Never double-escalates (only fires
  on `ACTIVE_AI`; handed-off conversations already skip turns).

## Primary user story

As a frustrated end user venting at a business's chat, I get handed to a
human the moment my messages turn sour — without having to explicitly
demand "let me talk to a person" — so the conversation de-escalates
instead of a bot persisting at me.

## Out of scope

- Operator dashboards/analytics of sentiment scores; storing per-message
  score history beyond the triggering handoff
- Hybrid prompt-nudge mechanism, keyword lists, non-LLM classifiers
- Per-tenant/per-channel threshold overrides (per-agent flag + global
  threshold only)
- Cooldowns / re-escalation tuning after an operator resumes
- Barge-in (#5), operator mini-page
- Webchat page or channel adapter changes

## Success demo

Done when: in `/chat` I send escalating frustrated messages ("isso não
resolve nada", "que atendimento horrível") and get no bot answer — just
the soft handoff message — while `GET /conversations/{id}` shows
`HUMAN_HANDOFF` with a `latest_handoff` brief citing the detected
frustration; tests cover below-threshold no-op, threshold trip, and
classifier failure proceeding with a normal turn.
