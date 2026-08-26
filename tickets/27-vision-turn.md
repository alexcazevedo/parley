---
id: 27
status: done
model: opus
depends_on: [26]
max_attempts: 3
files: [app/llm/base.py, app/llm/providers/openai_compatible.py, app/llm/providers/fake.py, app/agents/context.py, app/agents/runtime.py, app/conversations/turn.py, app/config.py, tests/test_vision_turn.py]
---
## Goal
The agent sees customer images: image messages reach a multimodal LLM as image content parts (caption as text), the turn's joined input carries readable placeholders, a non-vision deployment degrades to text, and an all-unsupported batch gets a fixed polite reply without an LLM call.

## Context
Read first: PRODUCT.md, app/llm/base.py (`LLMMessage`), app/llm/providers/
openai_compatible.py (`_message_to_wire`), app/llm/providers/fake.py,
app/agents/context.py (`ContextBuilder._history`), app/agents/runtime.py
(`_loop`: history + `input_text` fallback), app/conversations/turn.py
(`input_text = "\n".join(...)`), app/media/storage.py + app/models/
message.py (ticket 26: `media_key`, `media_mime`, `type`), tests/
test_context_builder.py + tests/test_agent_loop.py (fixtures to mirror).

LLM layer:
- `app/llm/base.py`: `@dataclass(slots=True) TextPart: text: str`;
  `@dataclass(slots=True) ImagePart: mime: str; data: bytes` (raw bytes;
  the provider base64-encodes); `ContentPart = TextPart | ImagePart`;
  `LLMMessage.content: str | list[ContentPart] | None`. Helper
  `text_of(content) -> str` (joins TextParts; used by anything that logs or
  measures content).
- Provider `_message_to_wire`: a `str` stays as today; a list becomes
  OpenAI parts: `{"type":"text","text":...}` /
  `{"type":"image_url","image_url":{"url":"data:{mime};base64,{b64}"}}`.
- `FakeLLMProvider` must accept list content (record it verbatim so tests
  can assert an `ImagePart` was sent).

Context builder: `ContextBuilder(image_loader: Callable[[Message],
Awaitable[bytes | None]] | None = None, vision_enabled: bool = True)` —
`build` becomes `async`. For a persisted `USER` message with
`type=="image"` and a `media_key`: when `vision_enabled` and the loader
returns bytes → content = `[ImagePart(media_mime, bytes),
TextPart(caption or "[imagem enviada pelo cliente]")]`; otherwise content =
`"[imagem enviada pelo cliente]" + (f": {caption}" if caption)`. `audio`
messages: content as stored (transcript, ticket 28) or
`"[áudio enviado pelo cliente]"` when empty. `unsupported`: content as
stored. Only the newest `settings.vision_max_images` (default 3) image
messages in the window carry bytes; older ones degrade to text (token
cost). Runtime wires `image_loader=lambda m: get_media_storage().get(
m.media_key)` and `vision_enabled=settings.llm_vision_enabled` (new
setting, default `True`); a loader exception → treated as `None` (warning
log), never fails the turn.

Turn input (`app/conversations/turn.py`): the joined `input_text` uses the
same text rendering per message type (extract a small `render_message_text
(message) -> str` in app/agents/context.py and use it in both places —
sentiment/summary keep reading `content`, unchanged). If EVERY message in
the batch is `type=="unsupported"`: skip the runtime, deliver
`UNSUPPORTED_MEDIA_REPLY = "Por enquanto só consigo entender texto, áudios
e imagens. Pode me mandar em texto?"` through `_deliver` (humanizer-aware,
like the escalation path), log `turn.unsupported_media_only`, return the
reply. Mixed batches run normally.

Tests `tests/test_vision_turn.py` (pipeline: upload PNG fixture via
`/channels/webchat/media` with caption → drain → `tick` with
`AgentRuntime` + `FakeLLMProvider`): (a) the provider's recorded request
has a USER message whose content list contains an `ImagePart` with
`mime=="image/png"` and the PNG bytes, plus a `TextPart` with the caption;
(b) `llm_vision_enabled=False` → no `ImagePart`, text contains "[imagem
enviada pelo cliente]: <caption>"; (c) loader raises → same degradation,
turn still replies; (d) 4 image messages → only 3 newest carry bytes;
(e) batch of one `unsupported` message (insert the row directly or via
adapter payload `type="unsupported"`) → provider NOT called, reply ==
`UNSUPPORTED_MEDIA_REPLY`; mixed text+unsupported → provider called;
(f) provider wire format unit test: list content serializes to the two
OpenAI part shapes with a valid data URL; (g) existing
tests/test_context_builder.py and tests/test_agent_loop.py green (adapt
call sites to `await build(...)`).

## Checks (run in order — cheapest first)
1. `uv run ruff check app tests`
2. `uv run python -c "import app.main"`
3. `docker compose up -d postgres && uv run pytest tests/test_vision_turn.py tests/test_context_builder.py tests/test_agent_loop.py -q`
4. `uv run pytest -q`
5. Live (stack rebuilt `docker compose up -d --build api worker && sleep 6`; real key; `LLM_MODEL` vision-capable, default gpt-4o-mini is): bootstrap, upload `tests/fixtures/order_label.png` with caption "o que está escrito aqui?" via `/channels/webchat/media`, wait ~20 s, `GET /conversations/{id}/messages` → an assistant message mentioning "12345".

## Done when
- The agent answers a question about a photo's content in the live probe;
  degradations and the unsupported-only reply are test-proven; suite green.

## Not included
- Audio transcription (28); Evolution (29); pages (30); OCR pre-step;
  image resizing/compression; per-agent vision toggle.
