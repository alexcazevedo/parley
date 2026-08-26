---
id: 26
status: done
model: sonnet
depends_on: []
max_attempts: 3
files: [app/media/__init__.py, app/media/storage.py, app/models/message.py, migrations/versions/0008_message_media.py, app/channels/base.py, app/channels/webchat.py, app/messages/ingest.py, app/api/webchat.py, app/api/media.py, app/api/conversations.py, app/main.py, app/config.py, docker-compose.yml, pyproject.toml, uv.lock, tests/test_media_ingest.py, tests/fixtures/order_label.png, tests/fixtures/voice_note.mp3]
---
## Goal
Channel-independent media ingest: a customer file (image/audio) becomes a stored asset plus a `Message` of type `image`/`audio`, uploadable through webchat and served back tenant-scoped — no LLM understanding yet.

## Context
Read first: PRODUCT.md, app/channels/base.py (`IncomingMessage`,
`ChannelAdapter.download_media` stub — remove it), app/channels/webchat.py,
app/messages/ingest.py, app/api/webchat.py (`ingest_message`, rate/size
limits), app/api/conversations.py (`MessageOut`), app/models/message.py,
migrations/versions/0007_agent_run_superseded.py (style), app/config.py,
docker-compose.yml (api/worker services), tests/test_ingest.py (helpers).

Types (`app/channels/base.py`):
- `@dataclass IncomingMedia: data: bytes; mime: str; filename: str | None
  = None`.
- `IncomingMessage.media: IncomingMedia | None = None`. `type` values used
  from now on: `"text" | "image" | "audio" | "unsupported"`. For `image`,
  `content` = caption or `""`; for `audio`, `content` = `""` (transcript
  filled later by ticket 28); for `unsupported`, `content` = a short label
  like `"[vídeo]"`.
- `MEDIA_TYPES = {"image", "audio"}`; `IMAGE_MIMES = {"image/jpeg",
  "image/png", "image/webp"}`; `AUDIO_MIMES = {"audio/ogg", "audio/mpeg",
  "audio/mp4", "audio/webm", "audio/wav", "audio/x-wav", "audio/mp3"}` —
  a mime with a `;codecs=` suffix is matched on the part before `;`.

Model + migration 0008 (`messages`): `media_key: str | None`
(`String(255)`), `media_mime: str | None` (`String(100)`). Downgrade drops
them.

Storage (`app/media/storage.py`): `class LocalMediaStorage(root: Path)`
with `async def put(tenant_id, data: bytes, mime) -> str` (key =
`f"{tenant_id}/{uuid4().hex}{ext}"`, ext from mime via a small map,
`aiofiles`-free: write with `asyncio.to_thread`), `async def get(key) ->
bytes`, `def path(key) -> Path` (rejects keys with `..` or leading `/`).
`get_media_storage()` singleton from `settings.media_root`. Settings:
`media_root: str = "./media"`, `media_max_bytes: int = 16 * 1024 * 1024`.
docker-compose: named volume `media_data` mounted at `/srv/media` on api
AND worker, `MEDIA_ROOT=/srv/media` in both `environment:` blocks.

Ingest (`app/messages/ingest.py`): if `incoming.media` is set, validate
mime ∈ IMAGE/AUDIO_MIMES per `incoming.type` and size ≤ `media_max_bytes`
(raise `MediaRejected(reason)` — new exception in ingest — for the API to
map to 415/413), `put()` it, and insert the message with `media_key`,
`media_mime`. Duplicate path unchanged (no second store: check the
duplicate first — do the `on_conflict` insert with a placeholder and only
`put()` when a row was inserted, then `UPDATE` the key; or look up the
duplicate before storing — either way no orphan file on duplicates).

Webchat: `WebChatAdapter.parse_incoming` accepts optional `payload["media"]`
(`IncomingMedia`) and `payload["type"]`; text path unchanged. New endpoint
`POST /channels/webchat/media` (multipart: `channel_id`, `user_id`, `file`
(UploadFile), optional `caption`, optional `message_id`) → same
`IngestOut`. Type derived from the file's content-type (image/* → `image`,
audio/* → `audio`, else 415). Same rate limit as text; `caption` goes
through `check_message_size`. Add `python-multipart` to dependencies
(`uv add python-multipart`, commit `uv.lock`).

Serve: `app/api/media.py` `GET /media/{message_id}` (tenant via
`get_current_tenant`): 404 unless the message belongs to the tenant and
has a `media_key`; returns `FileResponse(path, media_type=media_mime)`.
Register in `app/main.py`. `MessageOut` gains `media_url: str | None`
(`f"/media/{id}"` when `media_key` set) and `media_mime: str | None`.

Fixtures: generate once and commit `tests/fixtures/order_label.png`
(a 400x200 PNG with the text "PEDIDO 12345" — use Pillow via `uv run
--with pillow python -c ...`, do not add Pillow as a dependency) and
`tests/fixtures/voice_note.mp3` (any ≤100KB mp3; if no real audio is at
hand, a 1-second silent mp3 produced by `uv run --with pydub ...` or
ffmpeg is fine — tickets 28/30 mock STT).

Tests `tests/test_media_ingest.py`: (a) upload PNG via
`/channels/webchat/media` → 201, message row `type="image"`,
`media_mime="image/png"`, `content=""`, file exists under `media_root`
(point `MEDIA_ROOT` at a tmp dir via monkeypatched settings/env); with
`caption` → content = caption; (b) upload mp3 → `type="audio"`; (c)
`text/plain` file → 415; oversize (monkeypatch `media_max_bytes=10`) →
413; (d) `GET /media/{id}` returns the bytes with the right content-type;
other tenant → 404; text message id → 404; (e) `GET
/conversations/{id}/messages` shows `media_url`/`media_mime` for media
rows and `null` for text; (f) duplicate `message_id` upload → `duplicate:
true`, exactly one file stored; (g) text path untouched: existing
tests/test_ingest.py green.

## Checks (run in order — cheapest first)
1. `uv run ruff check app tests`
2. `uv run python -c "import app.main"`
3. `docker compose up -d postgres && uv run pytest tests/test_media_ingest.py tests/test_ingest.py -q`
4. `uv run pytest -q`
5. `docker compose up -d --build api worker && sleep 6 && B=$(curl -s -X POST localhost:8001/chat/bootstrap) && CH=$(echo "$B" | python3 -c "import sys,json;print(json.load(sys.stdin)['channel_id'])") && R=$(curl -s -F channel_id=$CH -F user_id=media-probe -F "file=@tests/fixtures/order_label.png;type=image/png" -H "X-API-Key: demo-webchat-key" localhost:8001/channels/webchat/media) && echo "$R" && MID=$(echo "$R" | python3 -c "import sys,json;print(json.load(sys.stdin)['message_id'])") && curl -sf -o /dev/null -w "%{http_code} %{content_type}\n" -H "X-API-Key: demo-webchat-key" localhost:8001/media/$MID | grep "200 image/png"`

## Done when
- A photo uploaded through webchat is stored, persisted as an `image`
  message, listed with `media_url`, and served back; suite green.

## Not included
- Transcription, vision, LLM/context changes (27, 28); Evolution media
  (29); page changes (30); S3/remote storage; retention.
