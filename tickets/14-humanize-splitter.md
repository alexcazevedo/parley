---
id: 14
status: done
model: sonnet
depends_on: []
max_attempts: 3
files: [app/conversations/humanize.py, app/config.py, tests/test_humanize.py]
---
## Goal
Pure splitting + pacing module: reply text → list of bubble parts, and a typing-delay function, fully unit-tested.

## Context
New file `app/conversations/humanize.py`, no I/O, no imports from channels/
turn (ticket 16 wires it). Two functions:

- `split_reply(text: str, max_parts: int) -> list[str]`
  1. Primary: split on blank lines (2+ consecutive newlines) — the agent is
     instructed (ticket 16) to write short paragraph-separated messages.
  2. Fallback: if that yields ONE part longer than ~280 chars, split it on
     sentence boundaries (`. ! ?` followed by space/uppercase) into chunks
     of roughly ≤280 chars, never mid-word.
  3. Fenced code blocks (``` … ```) are atomic: never split inside; a part
     containing a fence keeps the whole fence.
  4. Cap at `max_parts`: if over, merge the tail into the last part.
  5. Strip whitespace per part; drop empties; empty/whitespace input → `[]`.
- `typing_delay_seconds(part: str, settings) -> float` =
  `clamp(len(part) / settings.humanize_chars_per_sec,
  settings.humanize_min_delay_seconds, settings.humanize_max_delay_seconds)`.

Add to `app/config.py` Settings (with these defaults):
`humanize_chars_per_sec: float = 35.0`, `humanize_min_delay_seconds: float
= 0.8`, `humanize_max_delay_seconds: float = 4.0`, `humanize_max_parts:
int = 4`.

Tests `tests/test_humanize.py` (pure, no DB/redis needed): paragraph split,
wall-of-text sentence fallback, code-fence atomicity, cap+merge, empty
input, delay clamping both ends. Mirror existing test style
(`tests/test_debounce.py` for conventions).

## Checks (run in order — cheapest first)
1. `uv run ruff check app tests`
2. `uv run python -c "import app.conversations.humanize"`
3. `uv run pytest tests/test_humanize.py -q`

## Done when
- All listed behaviors covered by passing tests; module has zero
  dependencies beyond stdlib + Settings.

## Not included
- Any wiring into turn/adapters (ticket 16), sleeping, channel awareness.
