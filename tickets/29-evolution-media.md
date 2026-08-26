---
id: 29
status: done
model: sonnet
depends_on: [26]
max_attempts: 3
files: [app/channels/evolution.py, app/api/webhooks.py, tests/test_evolution_media.py, tests/fixtures/evolution_image_upsert.json, tests/fixtures/evolution_audio_upsert.json, tests/fixtures/evolution_video_upsert.json]
---
## Goal
WhatsApp photos and voice notes enter the same media pipeline: the Evolution adapter maps `imageMessage`/`audioMessage` payloads to `IncomingMessage` with media fetched from the Evolution API, and other media types become `unsupported` messages instead of being dropped.

## Context
Read first: PRODUCT.md, app/channels/evolution.py (`parse_incoming`,
`_connection`, httpx usage), app/api/webhooks.py (`whatsapp_webhook`),
app/channels/base.py + app/messages/ingest.py (ticket 26: `IncomingMedia`,
`MediaRejected`, types), tests/test_evolution.py + tests/fixtures/
evolution_messages_upsert.json (payload shape, respx patterns, helpers).

Parsing (`parse_incoming`, stays sync): after the `fromMe` check, inspect
`data.message`:
- `conversation` / `extendedTextMessage.text` → `type="text"` (as today).
- `imageMessage` → `type="image"`, `content = imageMessage.get("caption")
  or ""`, `media=None` for now, and `raw["_media"] = {"kind": "image",
  "mimetype": imageMessage.get("mimetype") or "image/jpeg"}`.
- `audioMessage` → `type="audio"`, `content=""`, `raw["_media"] =
  {"kind": "audio", "mimetype": audioMessage.get("mimetype") or
  "audio/ogg"}` (Evolution reports e.g. `audio/ogg; codecs=opus`).
- `videoMessage` / `documentMessage` / `stickerMessage` /
  `locationMessage` / `contactMessage` / `contactsArrayMessage` →
  `type="unsupported"`, `content` = `"[vídeo]"`, `"[documento]"`,
  `"[figurinha]"`, `"[localização]"`, `"[contato]"` respectively, no media.
- Anything else (reactions, protocol messages, empty) → `IgnoredEvent` as
  today.

Fetch (`EvolutionAdapter.fetch_media(self, incoming, channel) ->
IncomingMedia`, async): `POST {base_url}/chat/getBase64FromMediaMessage/
{instance}` headers `apikey`, json `{"message": {"key": {"id":
incoming.external_message_id}}, "convertToMp4": false}`; response json
`{"base64": "...", "mimetype": "..."}` → `IncomingMedia(data=b64decode,
mime=mimetype or raw["_media"]["mimetype"])`. `raise_for_status()`;
timeout 20 s.

Webhook (`app/api/webhooks.py`): after `parse_incoming`, if
`incoming.type in ("image", "audio")`: `incoming.media = await adapter.
fetch_media(incoming, channel)`; on any exception log
`webhook.media_fetch_failed` and convert the message to
`type="unsupported"`, `content="[mídia não recebida]"` (still persisted and
answered — never dropped). Then ingest as today; `MediaRejected` → log +
same conversion. `check_message_size` applies to `content` only. The size
limiter must not truncate `""`.

Fixtures: three `messages.upsert` payload files modeled on the existing
fixture with `messageType` and `message.imageMessage {url, mimetype:
"image/jpeg", caption: "olha isso", fileLength, ...}`, `message.
audioMessage {url, mimetype: "audio/ogg; codecs=opus", seconds: 4, ptt:
true}`, `message.videoMessage {mimetype: "video/mp4"}`.

Tests `tests/test_evolution_media.py` (webhook + respx on the Evolution
base64 endpoint returning the PNG/MP3 fixture bytes from ticket 26):
(a) image payload → 200, row `type="image"`, `content="olha isso"`,
`media_mime="image/jpeg"`, file stored, job enqueued; (b) audio payload →
`type="audio"`, `content=""`, `media_mime` normalized to `"audio/ogg"`
(strip `; codecs=…`); (c) video payload → 200, row `type="unsupported"`,
`content="[vídeo]"`, no Evolution media call, job enqueued; (d) base64
endpoint 500 → 200, row `type="unsupported"`, `content="[mídia não
recebida]"`; (e) unit: `parse_incoming` mapping for each of the five
unsupported kinds and `IgnoredEvent` for a `reactionMessage`; (f) existing
tests/test_evolution.py green — `test_non_text_message_is_ignored_with_200`
may need its payload changed to a reaction (a still-ignored kind); keep its
intent.

## Checks (run in order — cheapest first)
1. `uv run ruff check app tests`
2. `uv run python -c "import app.main"`
3. `docker compose up -d postgres && uv run pytest tests/test_evolution_media.py tests/test_evolution.py -q`
4. `uv run pytest -q`
5. `uv run pytest tests/test_media_ingest.py tests/test_ingest.py -q`

## Done when
- Evolution image/audio webhooks land as media messages with stored files
  through the same ingest as webchat; unsupported kinds are persisted and
  reach the turn; suite green.

## Not included
- Real Evolution instance probe (no instance in this environment);
  outbound media; media captions for documents; pages (30).
