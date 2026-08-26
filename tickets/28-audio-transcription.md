---
id: 28
status: done
model: sonnet
depends_on: [27]
max_attempts: 3
files: [app/media/stt.py, app/media/prepare.py, app/conversations/turn.py, app/config.py, tests/test_audio_turn.py]
---
## Goal
Voice notes are transcribed in the worker before the turn: the transcript becomes the audio message's content so the agent (and sentiment, summary, operator page) read it like text; STT failure degrades gracefully.

## Context
Read first: PRODUCT.md, app/conversations/turn.py (`process_turn` head:
state check → `_mark_read` → `input_text` join; ticket 27's
`render_message_text`), app/media/storage.py, app/models/message.py,
app/config.py, app/llm/providers/openai_compatible.py (httpx client/auth
pattern), tests/test_vision_turn.py (ticket 27 pipeline fixtures — mirror),
tests/test_http_tools.py (respx usage).

STT client (`app/media/stt.py`): `async def transcribe(data: bytes, mime:
str, *, settings) -> str` — multipart POST to
`f"{settings.stt_base_url or settings.llm_base_url}/audio/transcriptions"`
with `Authorization: Bearer {settings.stt_api_key or settings.llm_api_key}`,
fields `model=settings.stt_model` (default `"whisper-1"`), `file=(name,
data, mime)`, `response_format=json`, optional `language=settings.
stt_language` (default `"pt"`); timeout `settings.stt_timeout_seconds`
(default 20). Returns `response.json()["text"].strip()`. Raises `STTError`
on transport/HTTP/parse failure. New settings: `stt_base_url: str | None =
None`, `stt_api_key: str | None = None`, `stt_model`, `stt_language`,
`stt_timeout_seconds`.

Prepare step (`app/media/prepare.py`): `async def prepare_media_messages(
session, messages) -> None` — for each message with `type=="audio"`,
`media_key` set and empty `content`: load bytes from storage, `transcribe`,
set `content = transcript` and `raw_payload = {**(raw_payload or {}),
"transcript_status": "ok"}`; on `STTError` (or storage error) set `content
= AUDIO_FAILED_TEXT = "[áudio não compreendido]"`, `transcript_status:
"failed"`, log warning. Empty transcript → `"[áudio sem fala detectada]"`.
`await session.flush()` (the turn's commit persists it). Idempotent: rows
with non-empty content are skipped, so a re-run (barge-in requeue) never
transcribes twice.

Turn: call `await prepare_media_messages(session, messages)` in
`process_turn` right after the `ACTIVE_AI` check and before `_mark_read`/
`input_text`. `render_message_text` for audio then yields the transcript
(no placeholder needed beyond what ticket 27 defined for empty content).

Tests `tests/test_audio_turn.py` (upload `tests/fixtures/voice_note.mp3`
via `/channels/webchat/media`, STT mocked with respx on
`.../audio/transcriptions`): (a) STT returns `{"text": "esse é meu pedido
12345, quando chega?"}` → message content == that text, `transcript_status
== "ok"`, the `FakeLLMProvider` USER content includes it, reply produced;
(b) STT 500 → content == `AUDIO_FAILED_TEXT`, turn still replies, provider
called with the placeholder; (c) STT timeout (respx side effect
`httpx.ReadTimeout`) → same as (b); (d) re-running `prepare_media_messages`
on the same rows makes no second HTTP call (respx call count == 1);
(e) `stt_base_url`/`stt_api_key` overrides are used when set (assert the
request URL/header); (f) mixed batch text + audio → `input_text` contains
both in arrival order.

## Checks (run in order — cheapest first)
1. `uv run ruff check app tests`
2. `uv run python -c "import app.main"`
3. `docker compose up -d postgres && uv run pytest tests/test_audio_turn.py tests/test_vision_turn.py -q`
4. `uv run pytest -q`
5. Live (rebuilt stack, real OpenAI key): create a real voice note once with TTS — `curl -s https://api.openai.com/v1/audio/speech -H "Authorization: Bearer $LLM_API_KEY" -H "Content-Type: application/json" -d '{"model":"gpt-4o-mini-tts","voice":"alloy","input":"esse é meu pedido doze mil trezentos e quarenta e cinco, quando chega?"}' -o tests/fixtures/voice_note.mp3` (overwrite the silent placeholder, commit it) — then bootstrap, upload it via `/channels/webchat/media`, wait ~25 s, `GET …/messages`: the audio message content contains "12345" (or "doze mil") and an assistant reply mentions the order status.

## Done when
- A real voice note is transcribed and answered in the live probe; failure
  paths and idempotency test-proven; suite green.

## Not included
- Evolution audio fetch (29); pages (30); long audio chunking; streaming
  STT; speaker diarization; local Whisper.
