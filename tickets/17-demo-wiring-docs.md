---
id: 17
status: done
model: haiku
depends_on: [16]
max_attempts: 3
files: [app/api/chat.py, README.md, docs/ARCHITECTURE.md]
---
## Goal
Demo channel gets `humanize: true`; docs describe the humanizer.

## Context
`app/api/chat.py` `POST /chat/bootstrap` (read it first) seeds/looks up the
demo webchat channel. Change: on create, set channel `config={"humanize":
true}`; on the idempotent lookup path, if the existing channel's config
lacks the key, set it and commit (so pre-existing demo channels upgrade).
Mirror how `app/api/channels.py` handles the `config` field.

Docs (short, factual):
- `README.md`: one paragraph under the architecture blurb — humanizer =
  per-channel `config.humanize`; read receipt + composing presence
  (WhatsApp/Evolution), paced multi-bubble replies (all channels), pacing
  knobs in Settings (`humanize_*`).
- `docs/ARCHITECTURE.md`: a "Humanizer" section — flow (mark_read → split
  → per-part typing/delay/persist/send/commit), pointer to
  `app/conversations/humanize.py`, adapter hooks in
  `app/channels/base.py`, config knobs, and that `humanize` off keeps the
  legacy single-message path.

## Checks (run in order — cheapest first)
1. `uv run ruff check app`
2. `uv run python -c "import app.main"`
3. `docker compose up -d --build api worker && sleep 5 && curl -sf -X POST localhost:8001/chat/bootstrap | grep channel_id`
4. `docker compose up -d postgres && uv run pytest -q`

## Done when
- Bootstrap (fresh AND pre-existing tenant) yields a demo channel with
  `humanize: true`; docs mention the feature; suite green.

## Not included
- New pacing defaults, page changes, any pipeline logic.
