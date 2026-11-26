# Brief: answer vendezap customers with a parley agent

Run `/comb:auto` in the vendezap repository with this file as the brief. The grill is already done: the mode and every product decision are below, so there is nothing left to ask. The brief names the files it expects to change; read each before you plan. Vendezap's interface is pt-BR, so interface labels are quoted in pt-BR. Labels marked "suggested" do not exist yet. A `PRODUCT.md` for another milestone may already be in the vendezap repository: do not overwrite unfinished work; stop and say so if its tickets are not archived.

## What parley is, and where to read first

Parley is a headless service that runs AI conversations over HTTP. It has a channel type, `external`, for a project that keeps its own inbox and its own staff. The project forwards what customers and staff write. Parley runs the agent and pushes what the agent does back, signed. Parley sends nothing to the customer itself.

Read parley's own guide before planning anything: `docs/guides/project-channel.md` in the parley repository (`~/dev/parley` on this machine, [the guide](../guides/project-channel.md)). A running parley serves the same text: fetch `/llms.txt` on its base URL, then the guides it links, project-channel first. The guide owns every request, event and header format, the retry rules and the signature check; this brief only names them. `examples/project_inbox/app.py` in the parley repository is a working project of about 290 lines. Mirror its behaviour, not its storage.

## Decided on parley's side (do not re-ask)

- The project owns the inbox and the staff. Parley keeps a copy so the agent has context.
- A staff message pauses the agent for that conversation. So does a handoff by the agent. Only an explicit "hand back" resumes it. There is no automatic resume.
- Parley picks the department (a category defined in parley) and tells the project its id and name. When the project changes it, the project tells parley, and the agent stops choosing for that conversation.
- Agent replies are text only. Customer media in: image, voice note, PDF.
- One tenant per installation, as vendezap is one company per installation.

## Do not use `forward_url`

Vendezap's `Channel` model (`backend/app/channels/models.py`) has `forward_url`, `forward_headers` and `forward_events`. They relay raw Evolution webhook bodies to a previous webhook; `backend/app/webhooks/forward.py` sends them and `backend/app/webhooks/routes.py` picks the target. This integration does not use them, and parley must never connect to Evolution or to the WhatsApp numbers. Reasons:

- Those are Evolution message-upsert bodies. They have no author, so a staff message cannot be told from a message sent on the phone or from the agent's own reply.
- They carry no media bytes. Vendezap fetches media itself afterwards (`backend/app/media/service.py`), and parley needs the file.
- They are unsigned, in Evolution's format, and are not what the `external` channel accepts. The instance can also already forward to another system (the README mentions `Plugu`), and `forward_url` is where that goes.
- The numbers belong to vendezap. It must keep sending through Evolution itself.

Hook into vendezap's own code instead, after a message is stored.

## What vendezap must do, and where

Attendants reply to any conversa without claiming it: vendezap has no assignee anywhere in `backend/app/inbox/`. So the only way the agent learns a person has taken over is a staff message forwarded to parley. Every message an attendant sends must be forwarded.

1. **Settings.** Add to `backend/app/config.py` (and the README table): `PARLEY_URL`, `PARLEY_API_KEY`, `PARLEY_CHANNEL_ID`, `PARLEY_EVENTS_SECRET`. Empty `PARLEY_URL` means the feature is off and nothing else changes. Create the channel once (guide, step 2) and keep the secret: it is shown once and cannot be rotated. The `config.py` validator already demands 32 characters for the existing secrets when PUBLIC_URL is https; decide whether the new one follows it. The push URL parley calls must be a public name, or listed in parley's `CHANNEL_ALLOWED_HOSTS`.
2. **One parley channel per installation, not per number.** A vendezap `Conversation` (`backend/app/inbox/models.py`) is unique per `channel_id` and `contact_jid`: it already is a number plus a customer. Use its id as the parley `user_id`. A reply then comes back for one vendezap conversa and goes out through that conversa's number. Using the phone instead would merge a customer who writes to two numbers, and `Contact.phone` can be null (`@lid`). Per number would mean one secret, one feed position and one push URL each, for no gain. Contact merge moves conversations between contacts but keeps their ids.
3. **Forward customer messages.** Hook where `store_message` in `backend/app/inbox/service.py` creates a message that is not `from_me`; the Evolution webhook path (`backend/app/webhooks/service.py`) is the caller. Use `wa_message_id` as the parley `message_id`, so a repeated Evolution event is harmless on both sides. Text goes as a message. Image, voice note and PDF (`kind` `image`, `audio`, `document`) go as media (guide, step 9): the file arrives later, when `fetch_media` sets the media status to `ready`, so forward then, not when the row is created. Forwarding must never slow or fail the webhook, which must answer 200 to Evolution (see `evolution_webhook`).
4. **Forward attendant messages** as `sender` `staff` with the attendant's name (`User.name` through `author_id`): `send_text` and the media send in `backend/app/inbox/routes.py` (`send_media`). Parley takes no media from staff, so a staff file or photo needs a text stand-in (open question). Messages sent from the phone also arrive as `from_me` with no author; they are people too (see the open questions on the agent's own replies).
5. **Receive pushes.** A new route under `/api/` with no login, in `backend/app/webhooks/` and included in `backend/app/main.py`; the existing `/api/webhooks/evolution` route is the pattern (raw body read first, shared secret). Follow guide step 4: verify the signature on the raw body, apply the event, remember its `id`, answer 2xx fast. Apply `message.reply`, `conversation.handoff`, `conversation.handoff_brief`, `conversation.agent` and `conversation.category`; ignore unknown types.
6. **Catch up from the feed** (guide, step 5), as the example does: keep the feed position and the applied event ids in new tables (a new file in `backend/alembic/versions/`). Move the position only when the feed is read, never on a push. Read at start, and on a timer, because an event parley gave up on produces no signal. `lifespan` in `backend/app/main.py` already starts a background task for the catalog sync; a second one is the obvious place.
7. **Send the agent's reply.** For `message.reply`, call `send_text` (`backend/app/inbox/service.py`) for that conversa, through its own number, and store the message marked as sent by the agent. `author_id` is a nullable foreign key to `users`, and null already means "Enviado pelo celular" (`ChatPane.tsx`). Suggested: a new column on `Message` and a visible author "Agente", not a user row, because users are logins on the Usuários screen. Parley's `message_id` and the event `id` make a repeat harmless; vendezap needs its own guard so a repeated event does not send twice.
8. **Show agent on or off** in `frontend/src/components/inbox/ChatPane.tsx`, in the `chat-head` next to "Respondendo por {número}", with a suggested "Agente ativo" or "Agente pausado". Add the state to `ConversationOut` (`backend/app/inbox/schemas.py`) and to `frontend/src/api/inbox.ts`. Add a button "Devolver para o agente", shown only while paused; it calls parley's hand-back (guide, step 7). A 409 means the agent was already active: refresh the state.
9. **Department as an etiqueta.** On `conversation.category`, apply an etiqueta of that name to the conversa: the field "Etiquetas da conversa" in `ChatPane.tsx` is a `TagPicker` (`frontend/src/components/tags/TagPicker.tsx`); the data is `Tag` and `conversation_tags` (`backend/app/tags/models.py`), and the routes are in `backend/app/tags/routes.py`. Reuse an existing etiqueta of that name (names are unique ignoring case); otherwise create it. Replace the conversa's previous department etiqueta. Keep the mapping between parley's `category_id` and the etiqueta.
10. **Tell parley when an attendant changes it.** When the attendant adds, removes or swaps a mapped etiqueta in `ChatPane.tsx` (`addConversationTag`, `removeConversationTag` in `frontend/src/api/tags.ts`), the backend calls parley's category update (guide, step 8). Removing it sends a null category. Parley echoes a `conversation.category` event back with `source` `operator`; applying it must change nothing.
11. **Show the handoff brief.** On `conversation.handoff` (and the later `conversation.handoff_brief`), store the brief on the conversa and show it above the message list in `ChatPane.tsx`, with a suggested title "Resumo para quem assume". Only the attendants see it; it is never sent to the customer.

## Decided for vendezap (grill outcome, do not re-ask)

Mode: `mvp`. Routing profile `cloud`, sequential.

- **Which numbers.** A switch per number on the Números screen (`frontend/src/pages/ChannelsPage.tsx`), suggested label "Agente", off by default, stored on `Channel`. With the switch off, nothing for that number is forwarded and nothing changes.
- **Waiting.** While the agent is active a conversa never counts as "aguarda" (`waiting_since_map`, the counters on Hoje and in the sidebar). While it is paused, today's rule applies. A handoff makes the conversa wait at once, even when the agent spoke last. A conversa also waits while its forward is stuck or a reply could not be sent (next two items).
- **Parley unreachable.** Forwards go through a new table of pending forwards, sent by a background task in order per conversa, with retries. A webhook or an attendant is never blocked or failed by it. A customer message that is still unsent after 15 minutes is dropped from the queue (parley has no way to take a message without answering it), and the conversa counts as waiting. Staff messages are never dropped for age: they are what pauses the agent.
- **Number disconnected.** When `send_text` answers 409 for an agent reply, keep the reply pending and retry for 10 minutes. After that, drop it, store it in the conversa marked "não enviada" (suggested), visible to attendants only, and count the conversa as waiting. Never send a reply late.
- **Stand-ins.** What parley cannot take is forwarded as a short pt-BR text. Customer: `[O cliente enviou uma figurinha]`, `[O cliente enviou um vídeo]`, and the like for location, contact, kind `unsupported` and media in status `unavailable`. Attendant or phone: `[Atendente enviou uma imagem]` (or `um arquivo`, `um áudio`), followed by the caption if there is one. A staff stand-in pauses the agent like any staff message.
- **Messages from the phone.** A `from_me` message with no author that is not an agent reply is a person: forward it as `sender` `staff` with the name "Celular".
- **Switching on.** No history is forwarded; only messages stored after the switch. A conversa in which an attendant or the phone sent a message in the 24 hours before the switch starts paused: before its first forward, send its most recent staff message as `sender` `staff` (a staff message for a customer parley has not seen opens the conversation already paused). Every other conversa, and every new one, starts with the agent active.
- **Deploy.** Parley runs on the same VPS under its own public https name. `PARLEY_URL` is that https URL and parley pushes to `PUBLIC_URL`; no private-network setting on either side. `PARLEY_EVENTS_SECRET` follows the existing 32-character rule when `PUBLIC_URL` is https.

Decided from the code, with the reason:

- **The agent's own echo.** `store_message` upserts on `channel_id` and `wa_message_id`. Before calling Evolution, record the parley event `id` as "being sent" for the conversa; after `send_text` returns, record its `wa_message_id` as an agent message. A `from_me` row with no author is forwarded as staff only after a short delay (suggested 5 seconds) and only if its `wa_message_id` was not recorded as an agent message by then. That covers the echo arriving before or after `send_text` stores the row.
- **Order.** Apply events to a conversa in the order of the feed `seq`; a push that arrives ahead of a missing earlier event waits for the feed read. Serialise the work per conversa (a row lock on `Conversation`).
- **Push route.** Verify, store the event, answer 2xx; send to Evolution from the background task, not inside the request.
- **Departments.** A department name longer than 40 characters (`Tag.name`) is cut to 40. The mapping is by etiqueta id, so renaming an etiqueta keeps it mapped. Deleting a mapped etiqueta sends a null category for its conversas and removes the mapping. A department that parley removed leaves its etiqueta in place, unmapped.

## Out of scope

Catalog, pedido and kit tools for the agent, and a knowledge base. Replies with files or images. Closing conversations: vendezap has no closed state. Changes to the Evolution setup. Any change in the parley repository: ask for it instead.

## Success demo

With parley running and its stub model, a customer message sent to a vendezap number is answered in that conversa by "Agente", through the same number; an attendant's reply makes the conversa show "Agente pausado"; "Devolver para o agente" makes the next customer message get an answer; the department the agent picks appears as an etiqueta of the conversa and, when an attendant changes it, parley stops choosing; and after vendezap is stopped and started again, the replies it missed are sent once each.

Every browser spec in `e2e/tests/` (`m1.spec.ts` to `m11.spec.ts`, run by `scripts/e2e.sh`) stays green. The run uses a fake Evolution API (`e2e/fake_evolution.py`); add a fake parley in the same style for the new specs, and never call a real parley or Evolution from tests.
