---
id: 30
status: done
model: sonnet
depends_on: [27, 28, 29]
max_attempts: 3
files: [app/static/chat.html, app/static/operator.html, scripts/demo_media.py, scripts/mock_tool_server.py, README.md, docs/ARCHITECTURE.md]
---
## Goal
`/chat` can send a photo or a recorded/attached voice note; `/chat` and `/operator` render image thumbnails and audio players with transcripts; the PRODUCT.md success demo is scripted and passes.

## Context
Read first: PRODUCT.md, app/static/chat.html (`sendMessage`,
`renderMessages`, input row), app/static/operator.html (`renderMessages`),
app/api/webchat.py (`POST /channels/webchat/media` multipart: channel_id,
user_id, file, caption?, message_id?), app/api/conversations.py
(`MessageOut.media_url`, `media_mime`, `type`), app/api/media.py, scripts/
demo.py (style of the existing HTTP demo script), README.md, docs/
ARCHITECTURE.md.

`chat.html`: input row gains a 📎 button (hidden `<input type="file"
accept="image/*,audio/*">`) and a 🎤 button. 📎 → upload immediately with
the current text box value as `caption` (then clear it). 🎤 → toggle
recording via `MediaRecorder` (`audio/webm` default mime; when
`MediaRecorder.isTypeSupported("audio/webm;codecs=opus")` use it) — button
turns red with "Gravando… (clique para enviar)"; second click stops and
uploads the blob as `file` with filename `voice.webm`. Mic permission
denied or `MediaRecorder` missing → inline status line, no throw. Uploads
use `FormData` (no Content-Type header), `X-API-Key` as today, set
`conversationId`/`awaitingReply` from the response, then poll.
Rendering (both pages, same rules): `type=="image"` → `<img src=media_url>`
thumbnail (max 240px, click opens `media_url` in a new tab) + caption
text if any; `type=="audio"` → `<audio controls src=media_url>` + the
transcript text below (or the placeholder content); `type=="unsupported"`
→ chip with the content label. The `media_url` fetch needs the API key:
pages cannot set headers on `<img>`/`<audio>` — so fetch the media with
`fetch(media_url, {headers})`, turn it into an object URL
(`URL.createObjectURL`) once per message id (cache in a `Map`), and use
that as `src`. Revoke nothing (pages are short-lived). No external assets.

`scripts/demo_media.py` (host-side, plain `httpx`, like demo.py): bootstrap
→ upload `tests/fixtures/order_label.png` with caption "esse é meu pedido"
→ upload `tests/fixtures/voice_note.mp3` (within the debounce window, so
both land in one turn) → poll messages up to 60 s → print transcript →
assert: the audio message content is non-empty and not the failure
placeholder; at least one assistant message after the uploads mentions the
order (accept "12345" or "12.345" — Whisper formats pt-BR numbers with a
thousands separator) AND does not say the order was not found; both media
rows have `media_url`, and `GET media_url` returns 200.

Known from ticket 28's live probe: Whisper transcribes "doze mil trezentos
e quarenta e cinco" as "12.345", so the agent may call `get_order` with
`order_id="12.345"` and the mock returns not-found. Fix in
`scripts/mock_tool_server.py`: normalize `order_id` by stripping every
non-digit character before the lookup (so "12.345", "12 345", "#12345" all
resolve to "12345"); keep the response shape unchanged. The demo pairs the
photo (which literally shows "PEDIDO 12345") with the voice note in the
same debounce window, so the agent has both.
Exit 0/1/2 like demo.py (2 when the LLM key is the placeholder).

Docs: README — "Run it" mention of the 📎/🎤 buttons and
`scripts/demo_media.py`; "API overview" rows for `POST
/channels/webchat/media` and `GET /media/{message_id}`; update the "Known
MVP limitations" text that says the harness is text-only (if present).
docs/ARCHITECTURE.md — new "## Inbound media" section: channel-independent
pipeline (adapter → `IncomingMedia` → storage → `Message.type` →
worker `prepare_media_messages` STT → `ContextBuilder` image parts),
degradations, settings (`MEDIA_ROOT`, `LLM_VISION_ENABLED`, `STT_*`),
storage volume; plus one line each under "Modules" for `app/media/*` and
`app/api/media.py`.

## Checks (run in order — cheapest first)
1. `uv run ruff check app tests scripts`
2. `uv run python -c "import app.main"`
3. `docker compose up -d postgres && uv run pytest -q`
4. `docker compose up -d --build api worker && sleep 6 && curl -sf localhost:8001/chat | grep -c "MediaRecorder" && curl -sf localhost:8001/operator | grep -c "createObjectURL"`
5. `uv run python scripts/demo_media.py` (real LLM key in .env) — exit 0 and printed transcript shows the image + audio rows and an assistant reply mentioning 12345.
6. Browser check if a browser tool is available: open http://localhost:8001/chat, attach `tests/fixtures/order_label.png`, confirm the thumbnail renders and a reply arrives; open http://localhost:8001/operator, select that conversation, confirm the thumbnail and the audio player + transcript render. If no browser tool: say so explicitly in the report.

## Done when
- `scripts/demo_media.py` passes end-to-end against the live stack; both
  pages render media; docs updated; suite green.

## Not included
- Outbound media; drag-and-drop; image compression client-side; mobile
  polish; retention/cleanup; Evolution live probe.
