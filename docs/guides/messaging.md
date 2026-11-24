# Messaging

Send a customer message, receive the reply, on web chat and on WhatsApp through Evolution. Start from the variables of [setup](setup.md): `$BASE_URL`, `$TENANT_KEY`, `$AGENT_ID` and `$CHANNEL_ID`. The `worker` service must be running and the model reachable (a valid model key, or [the stub](quickstart.md#try-it-without-a-model-key)). When the model call fails, the customer is sent a failure text instead of a reply (see the table at the end of this guide). Errors, paging and limits are in [api-conventions](api-conventions.md).

Variables added here:

```
USER_ID=customer-1
```

`$CONVERSATION_ID` and `$LAST_SEQ` are captured with `jq` in steps 1 and 2.

Every request sends `X-API-Key: $TENANT_KEY`. Parley has no push transport: a client learns about replies by polling.

## How a conversation works

- A customer is a contact: `external_user_id` is the `user_id` you send on web chat and the phone number on WhatsApp. The first message creates the contact and a conversation.
- A conversation is reused while its state is not `CLOSED`. After it is closed, the next message from the same customer opens a new conversation with a new id. Keep the `conversation_id` the last send returned.
- Every message is stored with a `seq`. Within one conversation `seq` only grows, but values are not consecutive. Use it as the polling position.
- While the state is `HUMAN_HANDOFF` or `CLOSED` the agent does not reply to new messages. See [operators](operators.md).
- A handoff by the agent arrives as an ordinary `assistant` message with the agent's `handoff_message`; nothing in the message marks it. A customer-side client detects a handoff by reading `state` from `GET /conversations/{conversation_id}`: `HUMAN_HANDOFF` means a person has it, `CLOSED` means it is finished.
- `last_message_at` on the conversation is `null` after the first customer message. It is set when a reply is stored: the agent's (including a handoff text or a failure text) or an operator's. Customer messages never set it. The inbox sorts by it, conversations with `null` last.

## 1. Send a web chat message

`POST /channels/webchat/messages`:

| Field | Required | Rule |
|---|---|---|
| `channel_id` | yes | `$CHANNEL_ID` of a `webchat` channel. 404 `channel not found`; 422 `channel is not a webchat channel`. |
| `user_id` | yes | Your id for the customer. Any string. Not validated. |
| `message` | yes | Text, at most `MAX_MESSAGE_CHARS` characters (413 over it, nothing stored). |
| `message_id` | no | Your id for this message. Sending the same `message_id` on the same channel again stores nothing and returns `"duplicate": true`. It is unique per channel, not per `user_id`: two customers must not share one. Omitted, parley makes one up. |

```
SENT=$(curl -s -X POST $BASE_URL/channels/webchat/messages \
  -H "X-API-Key: $TENANT_KEY" -H "Content-Type: application/json" \
  -d "{\"channel_id\": \"$CHANNEL_ID\", \"user_id\": \"$USER_ID\", \"message\": \"Hi, where is my order?\"}" | jq .)
CONVERSATION_ID=$(echo "$SENT" | jq -r .conversation_id)
```

Expected: 201 with `conversation_id`, `message_id` and `duplicate` (in `$SENT`). `$CONVERSATION_ID` is the `conversation_id`. The reply is not in this response.

Over the rate limit the response is 429 and nothing is stored (see [api-conventions](api-conventions.md)).

Check: `duplicate` is `false`, and `curl -s $BASE_URL/conversations/$CONVERSATION_ID -H "X-API-Key: $TENANT_KEY"` returns 200 with `state` `ACTIVE_AI`.

## 2. Poll for the reply

`GET /conversations/{conversation_id}/messages` returns the conversation's messages, oldest first. Query: `after_seq` (only messages with a higher `seq`), `after` (only messages created after that time), `include_tools` (default `false`), `limit` and `cursor` (see [api-conventions](api-conventions.md)).

Set the starting position once, right after step 1. Without `after_seq` the response is the newest `limit` messages:

```
LAST_SEQ=$(curl -s "$BASE_URL/conversations/$CONVERSATION_ID/messages" -H "X-API-Key: $TENANT_KEY" | jq '[.[].seq] | max // 0')
```

Then repeat, once per second, until a reply arrives or the time limit under "How long a reply takes" below has passed:

```
curl -s "$BASE_URL/conversations/$CONVERSATION_ID/messages?after_seq=$LAST_SEQ" -H "X-API-Key: $TENANT_KEY"
```

After each response, set `$LAST_SEQ` to the highest `seq` in it (leave it unchanged when the list is empty). If the response has an `X-Next-Cursor` header, there are more messages: repeat with `cursor` and the same `after_seq`.

Message fields:

| Field | Meaning |
|---|---|
| `id`, `seq`, `conversation_id`, `channel_id` | Identity and position. |
| `direction` | `in` (from the customer) or `out` (to the customer). |
| `role` | `user` (customer), `assistant` (the agent), `human_operator` (an operator, see [operators](operators.md)). `tool` and `system` rows exist but tool rows are hidden unless `include_tools=true`. |
| `type` | `text`, `image`, `audio` or `unsupported`. With `include_tools=true` also `tool_call` and `tool_result`. |
| `content` | The text. For an image, its caption. For an audio message, the transcript once the agent's turn has started (empty before). For `unsupported`, a label such as `[vídeo]`. |
| `external_message_id` | The customer's `message_id`. `null` on replies. |
| `media_url`, `media_mime` | Set when a file is stored: `media_url` is `/media/{message_id}`. Otherwise `null`. |
| `created_at` | Time. |

The reply is the new message with `role` `assistant` and `direction` `out`.

Check: within the time below, the response contains a message with `role` `assistant`, and its `content` is the reply text.

### How long a reply takes

1. Parley waits `DEBOUNCE_SECONDS` (default 3) after the customer's last message. Each new message from the same customer restarts the wait, and a burst is answered once, as one batch.
2. The worker then runs the agent: a model call per step, plus a call for each optional check that is on (categories, sentiment, boundaries). The agent run is cut off after 60 seconds; the customer then gets the agent's `error_message`.
3. With `humanize` on the channel, the reply is split into up to `HUMANIZE_MAX_PARTS` messages. Each is stored when it is sent, after a typing pause of `HUMANIZE_MIN_DELAY_SECONDS` to `HUMANIZE_MAX_DELAY_SECONDS` (longer text pauses longer, at `HUMANIZE_CHARS_PER_SEC`). Bubbles therefore appear one by one.
4. If the customer writes again while a reply is being prepared, the unsent part is dropped; the newer message gets its own turn.

How to poll:

- Poll every 1 second.
- Without `humanize`: stop at the first `assistant` message after your own.
- With `humanize`: after the first `assistant` message, keep polling until `HUMANIZE_MAX_DELAY_SECONDS` plus 3 seconds pass with no new message. Then the reply is complete. There is no marker for the last bubble.
- Give up after 3 minutes with no `assistant` message. A failed turn is retried twice before the customer gets the failure text, which fits in that time. Then check: `GET /conversations/{conversation_id}` for `state` (a conversation in `HUMAN_HANDOFF` or `CLOSED` never gets a reply), `docker compose ps worker`, and `docker compose logs worker`.

## 3. Send an image or a voice note on web chat

`POST /channels/webchat/media` is a multipart form with `channel_id`, `user_id`, `file`, and optionally `caption` (at most `MAX_MESSAGE_CHARS`) and `message_id`.

```
curl -s -X POST $BASE_URL/channels/webchat/media \
  -H "X-API-Key: $TENANT_KEY" \
  -F "channel_id=$CHANNEL_ID" -F "user_id=$USER_ID" -F "caption=my receipt" \
  -F "file=@receipt.jpg"
```

Expected: 201 with `conversation_id`, `message_id`, `duplicate`, as in step 1. Poll as in step 2.

- Parley decides the type from the file's bytes and ignores the content type you send. Images: JPEG, PNG, WebP. Audio: Ogg, MP3, MP4 audio, WebM, WAV. Anything else is 415 `unsupported media content`; nothing is stored and the customer receives nothing.
- A file over `MEDIA_MAX_BYTES` is 413 `media exceeds max size`. No `Content-Length` is needed: parley counts the bytes it receives, and a body over `MEDIA_MAX_BYTES` plus 1 MiB is cut off with 413 `request body too large`.
- A voice note is transcribed before the agent answers (`STT_MODEL`, `STT_LANGUAGE`). If transcription fails, the agent reads `[áudio não compreendido]` instead and still answers.
- An image is sent to the model when `LLM_VISION_ENABLED` is `true` (the newest `VISION_MAX_IMAGES` of them); otherwise the model reads a placeholder.
- Fetch the stored file with `GET /media/{message_id}` and `X-API-Key` (the id is the message's `id`; `media_url` is that path). 404 `media not found` when the message has no file.

Check: `curl -s "$BASE_URL/conversations/$CONVERSATION_ID/messages?limit=1" -H "X-API-Key: $TENANT_KEY"` shows `type` `image` or `audio` and a `media_url`.

## 4. WhatsApp through Evolution: configure the channel

Create the channel as in step 4 of [setup](setup.md): `type` `whatsapp_evolution`, `config` with `base_url` and `instance`, and `secret` set to the Evolution API key. Both config keys are needed to send; the create call does not check that they are present.

The address rule for `base_url`:

- It must be `http` or `https`. At create, the name is checked without DNS: `localhost`, names ending `.local`, `.localhost` or `.internal`, and private, loopback or link-local addresses give 422 `invalid base_url: ...`.
- On every call parley also resolves the name and refuses it unless every address is public. A host that passes at create can still be refused later.
- `CHANNEL_ALLOWED_HOSTS` (comma-separated host names) lists hosts that skip these address checks. Use it for an Evolution server on a private network or on the compose network, for example `evolution`. The `TOOL_ALLOWED_HOSTS` and `TOOL_SSRF_DISABLED` settings do not apply to channels.
- When the address is refused: the agent does not run and the customer gets no reply (a warning is logged); an operator reply gets 502 `channel endpoint refused`.

Check: `curl -s $BASE_URL/channels/$CHANNEL_ID -H "X-API-Key: $TENANT_KEY"` shows `type` `whatsapp_evolution` and the `base_url` and `instance` you set.

## 5. WhatsApp: register the webhook in Evolution

Set the Evolution instance to call parley with:

- URL: `POST $BASE_URL/webhooks/whatsapp/$CHANNEL_ID` (write out the values). `$BASE_URL` must be an address Evolution can reach.
- Event: `messages.upsert`. The body's `event` field must equal `messages.upsert`; every other event is acknowledged and ignored.
- Header `apikey` equal to the channel's `secret`. Parley compares it with the stored secret. A missing or different value is 401 `invalid apikey`. A channel created without a `secret` always gives 401. An unknown `channel_id` is 404 `channel not found`.
- The body is Evolution's usual JSON: parley reads `data.key.id` (required), `data.key.remoteJid`, `data.key.fromMe`, `data.message` and `data.messageTimestamp`. A body with no `data.key.id` is a 500.

The webhook needs no tenant key: the channel id and `apikey` identify the tenant. It answers 200 with a `status`:

| `status` | Meaning |
|---|---|
| `ok` | Stored and queued, or a repeat of a message id already stored (nothing done). |
| `ignored` | Not for the agent. See below. |
| `rate_limited` | Over the inbound limit. The message is stored (operators see it) but no agent turn runs. Never a 429. |

Ignored: any event other than `messages.upsert`; messages you sent yourself (`fromMe`); group, broadcast and newsletter chats; an empty or unknown `remoteJid` suffix; message kinds other than text, image, audio, video, document, sticker, location and contact.

How the customer is identified: a `remoteJid` ending `@s.whatsapp.net` gives `external_user_id` equal to the number without the suffix; one ending `@lid` keeps the full value. Replies go to that `external_user_id` as `number`. A reply to an `@lid` id is sent to that id as it is; whether Evolution delivers it has not been checked against a real instance, so test it once. A person seen under both an `@lid` and a phone number is two contacts.

Test it without Evolution:

```
EVOLUTION_KEY=<the channel secret>
curl -s -X POST $BASE_URL/webhooks/whatsapp/$CHANNEL_ID \
  -H "apikey: $EVOLUTION_KEY" -H "Content-Type: application/json" \
  -d '{"event": "messages.upsert", "data": {"key": {"id": "test-1", "remoteJid": "5511999990000@s.whatsapp.net", "fromMe": false}, "message": {"conversation": "hello"}, "messageTimestamp": 1700000000}}'
```

Expected: 200 `{"status": "ok"}`. Sending the same command again also answers `ok` and stores nothing.

Check: `curl -s "$BASE_URL/conversations?limit=100" -H "X-API-Key: $TENANT_KEY"` lists a conversation whose `contact_external_user_id` is `5511999990000` (the list has no filter by customer, and a conversation with no reply yet sorts after the active ones). Keep its `id` as `$CONVERSATION_ID` and read its messages as in step 2. The reply goes to Evolution, so the test customer receives it only if the number exists on that instance.

Text over `MAX_MESSAGE_CHARS` is cut to the limit, not refused. Replies and operator messages are sent with the Evolution calls below.

## 6. WhatsApp: what parley calls on Evolution

All calls go to `base_url` with header `apikey` set to the channel secret and `instance` as the last path part:

| Call | When |
|---|---|
| `POST {base_url}/message/sendText/{instance}`, body `{"number": ..., "text": ...}` | Every reply, handoff text, failure text and operator message. |
| `POST {base_url}/chat/getBase64FromMediaMessage/{instance}`, body `{"message": {"key": {"id": ...}}, "convertToMp4": false}` | Before the agent answers a message that holds an image or voice note. The answer must be JSON with `base64`, and `mimetype` if known. |
| `POST {base_url}/chat/markMessageAsRead/{instance}` | Only with `humanize` on: a read receipt for the customer's newest message. A failure is logged and ignored. |
| `POST {base_url}/chat/sendPresence/{instance}`, `"presence": "composing"` | Only with `humanize` on: typing indicator before each part. |

Media on WhatsApp: the webhook stores an image or voice note without the file and answers at once; the worker downloads it just before the turn. The same type and size rules as step 3 apply (`MEDIA_MAX_BYTES`, 20 seconds per request, 30 seconds in all). The response is read up to about 4/3 of `MEDIA_MAX_BYTES` plus 64 KiB (base64 and the JSON around it), and a response with a `Content-Encoding` other than identity is refused; either is stored as described next. If the download fails or the file is rejected, the message becomes `unsupported` with content `[mídia não recebida]`. It is not retried. An image or voice note that arrives over the rate limit is stored the same way and never downloaded.

Check: after a real image arrives, `GET /conversations/{conversation_id}/messages` shows `type` `image` and a `media_url` once the agent's turn has started.

## What the customer receives when something goes wrong

| Situation | The customer receives |
|---|---|
| The agent run fails or passes 60 seconds | The agent's `error_message`, else `Desculpe, tive um problema técnico. Pode tentar novamente?` Sent as a normal reply. |
| The agent used `max_tool_iterations` rounds without answering | `Desculpe, não consegui concluir. Vou encaminhar.` Fixed text. It does not hand the conversation to a human. |
| The model answers with nothing | `Desculpe, não entendi. Pode reformular?` Fixed text. |
| Everything in the batch is video, document, sticker, location or contact (WhatsApp) | The agent's `unsupported_media_message`, else `Por enquanto só consigo entender texto, áudios e imagens. Pode me mandar em texto?` No model call. A batch that mixes these with text or images is answered normally; the unsupported item reaches the model as a label. |
| The turn fails outside the agent run (database, sending) | The turn is retried after 2 seconds, then after 8 more. After the third failure the customer gets the `error_message` text once. If part of a reply was already sent, there is no retry and no notice. No notice either when a human has the conversation. |
| A reply is blocked by boundaries | `boundary_refusal_message`. See [agent-behaviour](agent-behaviour.md). |
| Web chat upload is not a supported image or audio | Nothing: the upload gets 415 and nothing is stored. |

Check: to see a failure text, set `error_message` with `PATCH /agents/{agent_id}`, start the worker with a wrong `LLM_API_KEY`, and send a message. The reply is that text.

Next: hand conversations to people in [operators](operators.md), and tune the agent in [agent-behaviour](agent-behaviour.md).
