---
id: 09
status: done
model: sonnet
depends_on: [03]
max_attempts: 3
files:
  - app/channels/evolution.py
  - app/channels/registry.py
  - app/api/webhooks.py
  - app/main.py
  - tests/test_evolution.py
  - tests/fixtures/evolution_messages_upsert.json
---
## Goal
WhatsApp channel via Evolution API: authenticated webhook ingestion + outbound `sendText`, behind `ChannelAdapter`.

## Context
Channel `config`: `{base_url, instance}`; `secret_encrypted` = Evolution `apikey`. Webhook `POST /webhooks/whatsapp/{channel_id}` — no tenant key; validate header `apikey` equals decrypted channel secret → else 401. Respond 200 immediately after enqueue; return 200 (not error) for ignored events.

Parse Evolution v2 `messages.upsert` payload: `event == "messages.upsert"`, `data.key.remoteJid` (strip `@s.whatsapp.net` → external_user_id), `data.key.id` → external_message_id, `data.key.fromMe == true` → ignore, `data.pushName` → display_name, text from `data.message.conversation` or `data.message.extendedTextMessage.text`; non-text → ignore (log). `data.messageTimestamp` epoch → timestamp. Put a realistic sample in `tests/fixtures/`.

`EvolutionAdapter.send_message`: POST `{base_url}/message/sendText/{instance}` headers `apikey`, body `{"number": external_user_id, "text": content}` via httpx, timeout 10s; raise on non-2xx (caller logs, job retry handles). Register in `registry.py` for type `whatsapp_evolution`.

Tests: `respx` for send; webhook via TestClient with fixture.

## Checks (run in order — cheapest first)
1. `uv run ruff check .`
2. `uv run pytest -q tests/test_evolution.py`
3. `uv run pytest -q`

## Done when
- Fixture POSTed with correct `apikey` → Message + job; wrong key → 401; `fromMe` → 200 no row; duplicate `key.id` → no second row.
- `send_message` hits `/message/sendText/{instance}` with expected body.

## Not included
- Media download, read receipts, instance creation/QR pairing, other Evolution events.
