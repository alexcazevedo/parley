---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
---

# Inbound media — audio & images

## What / why

Customers on WhatsApp send voice notes and photos as naturally as text.
Today every non-text message is silently dropped at the adapter
(`IgnoredEvent("non-text message")`) and the webchat cannot send media at
all — the customer gets no reply and no explanation. The harness already
has the slot (`Message.type`, `IncomingMessage.type`) but nothing behind
it.

Shape (decided during grilling):

- **Channel-independent core.** Media handling is one pipeline in core:
  ingest stores the file and a `Message` of type `audio` / `image`; audio
  is transcribed to text (STT) and the transcript becomes the message
  content; images are handed to the agent as image content on the turn
  (multimodal LLM), with the customer's caption as text. Adapters only map
  their payloads into `IncomingMessage` — no channel gets special
  behavior in the agent loop, humanizer, sentiment, summary or barge-in.
- **Both adapters wired**: Evolution (WhatsApp audio/image, fetched from
  the Evolution API) and webchat (an upload path so `/chat` can send a
  photo or a recorded/attached audio). Same core code path for both.
- **Debounce/batch semantics unchanged**: a burst of text + photo + voice
  note is still one turn; the joined input carries text, transcripts and
  images in arrival order.
- **Transcripts show the media**: `/chat` and `/operator` render image
  messages as inline thumbnails and audio messages as a player plus the
  transcript text, served by a tenant-scoped media endpoint.
- **Graceful degradation**: STT failure → message kept as `audio` with a
  placeholder content and the agent told a voice note could not be
  understood; a non-vision model → image described to the agent as
  "[imagem enviada pelo cliente]" so the turn still answers. Unsupported
  types (video, documents, stickers) get a persisted message of their
  type and a fixed polite reply; nothing is silently dropped anymore.
- **Inbound only**: the agent and operators reply in text.

## Primary user story

As a customer, I send a voice note or a photo (e.g. of a damaged product
or an order label) instead of typing, and the agent answers as if I had
written it — and the operator who takes over can see and hear exactly
what I sent.

## Out of scope

- Outbound media (TTS voice replies, agent/operator sending images)
- Video, documents, stickers, locations, contacts beyond the polite
  fallback reply
- Media retention policies, virus scanning, image moderation
- OCR as a separate step (the vision model reads text in images)
- Long-audio handling (> a few minutes), streaming transcription
- Semantic search over media; storing media in long-term memory

## Success demo

Done when: in `/chat` I attach a photo of an order label and record/attach
a voice note saying "esse é meu pedido, quando chega?", and the agent
replies with the order status read from the image and the question from
the audio, the transcript in `/chat` and `/operator` shows the thumbnail,
the player and the transcript text; plus tests proving the Evolution
adapter maps real `audioMessage` / `imageMessage` payloads into the same
pipeline, the debounce batch order, STT-failure and non-vision-model
degradation, and that unsupported types get the fallback reply.
