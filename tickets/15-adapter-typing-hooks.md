---
id: 15
status: done
model: sonnet
depends_on: []
max_attempts: 3
files: [app/channels/base.py, app/channels/evolution.py, tests/test_evolution.py]
---
## Goal
Optional `mark_read` / `send_typing` hooks on `ChannelAdapter`, implemented for Evolution (WhatsApp), no-op everywhere else, proven by mocked-HTTP tests.

## Context
`app/channels/base.py`: add two non-abstract async methods to
`ChannelAdapter`, both default no-op (`return None`):

- `mark_read(self, external_message_id: str, contact: Contact, channel:
  Channel) -> None`
- `send_typing(self, contact: Contact, channel: Channel, duration_seconds:
  float) -> None`

`app/channels/evolution.py` `EvolutionAdapter` implements both, reusing the
same config/apikey extraction as `send_message` (base_url, instance,
decrypted apikey):

- `mark_read` → `POST {base_url}/chat/markMessageAsRead/{instance}` json
  `{"readMessages": [{"remoteJid": f"{contact.external_user_id}@s.whatsapp.net",
  "fromMe": false, "id": external_message_id}]}`
- `send_typing` → `POST {base_url}/chat/sendPresence/{instance}` json
  `{"number": contact.external_user_id, "presence": "composing",
  "delay": int(duration_seconds * 1000)}`

Both raise on HTTP error like `send_message` does (caller decides
best-effort handling — that's ticket 16, not here). httpx client timeout 10,
`apikey` header, matching existing style.

Tests: READ `tests/test_evolution.py` first and mirror its existing HTTP
mocking approach exactly (however it mocks `send_message`'s httpx calls).
Add tests asserting URL, headers, and JSON body for both new methods, plus
one test that `WebChatAdapter` (via base class) no-ops without error.

## Checks (run in order — cheapest first)
1. `uv run ruff check app tests`
2. `uv run python -c "import app.main"`
3. `docker compose up -d postgres && uv run pytest tests/test_evolution.py -q`
4. `uv run pytest -q`  (full suite regression — behavior must be unchanged)

## Done when
- Both hooks send the exact documented Evolution payloads (asserted in
  tests); default adapters unaffected; full suite green.

## Not included
- Calling these hooks from the turn pipeline (ticket 16); webchat typing
  transport; presence states other than "composing".
