# STATE — rewrite after every ticket completes; never append. ≤30 lines total.

## Now
inbound-media mvp run COMPLETE 2026-08-25 — tickets 26–30 done (1 attempt each); suite 197 green; scripts/demo_media.py passes live (photo + voice note → "Seu pedido 12345 foi despachado"); browser check done by orchestrator: /operator renders thumbnail, audio player, transcript; /chat shows 📎/🎤. Awaiting final diff review (mvp approval 2 of 2).

## Next
- final diff review with user → commit + push

## Decisions
- core channel-agnostic: adapters emit IncomingMessage(type, content, media); STT in worker before turn (idempotent); images as ImagePart bytes (newest 3), older/no-vision → text placeholder
- media on messages (media_key, media_mime; mig 0008); LocalMediaStorage under MEDIA_ROOT; compose volume media_data; `media/` gitignored
- Whisper pt-BR renders "12.345" → mock_tool_server normalizes order_id digits (demo-only fix; real tools should normalize too)
- Evolution media via POST /chat/getBase64FromMediaMessage; fetch/ingest failure → unsupported "[mídia não recebida]", never dropped
- new dep: python-multipart; fixtures order_label.png + voice_note.mp3 (96KB TTS) committed
- prior: operator-ui ad362e1, barge-in cd29d41; repo github.com/alexazevedo/conversational-harness; host ports 5434/6380/8001/9000

## Blockers
- none
