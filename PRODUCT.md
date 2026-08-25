---
mode: mvp                      # prototype | mvp | production
routing_profile: cloud         # cloud | cloud+local (local is opt-in, never a default)
parallel_default: sequential   # sequential | parallel
---

# Operator UI — handoff inbox

## What / why

Every operator action already exists as an API (list conversations,
transcript, operator reply, handoff / resume / close, latest warm-handoff
brief), but the only human-facing surface is the end-user `/chat` page.
Demoing a handoff today means curl. An operator page closes the loop: the
human side of "AI hands off, person takes over, hands back" becomes
visible and clickable, which is the whole point of the warm-handoff,
sentiment-escalation and barge-in work already shipped.

Shape (decided during grilling):

- **Single self-contained static page** served at `/operator`, same style
  as `/chat` (no build step, no framework, plain HTML/JS, polling).
- **Access**: the page asks once for the tenant `X-API-Key` (prefilled with
  the demo key), remembers it in `localStorage`, and uses it for every
  call. No new auth code, no operator accounts.
- **Inbox (primary)**: conversations in `HUMAN_HANDOFF`, newest activity
  first, each showing the latest handoff's `requested_by`, reason and the
  operator brief (`summary`). Opening one shows the full transcript
  (user / assistant / operator messages, tool messages hidden) and lets the
  operator: send a reply (delivered through the channel adapter), **resume**
  (hand back to the AI) or **close**.
- **Ativas (secondary tab)**: `ACTIVE_AI` / `WAITING` conversations,
  read-only transcript, one **Assumir** button that forces a handoff
  (`requested_by=operator`) and moves it to the inbox.
- Lists and the open transcript refresh by polling, like `/chat` does.
- Existing endpoints only; any API change is limited to what the lists need
  (e.g. filtering by state, ordering) and stays backward compatible.

## Primary user story

As a support operator, I open `/operator`, see the conversations the AI
handed to me with a brief of what happened, answer the customer myself, and
hand the conversation back to the AI (or close it) — without touching curl
or the database.

## Out of scope

- Operator accounts, roles, login, per-operator assignment
- WebSocket/SSE streaming (polling is enough)
- Editing agents/tools/channels/contacts/memories from the page
- Viewing AI runs, tool calls or sentiment scores (supervision/debug lens)
- Notifications (sound, browser push), unread counters across tabs
- Mobile layout polish, i18n (page copy in pt-BR like `/chat`)

## Success demo

Done when: with the stack up, I ask for a human in `/chat` (or trip
sentiment escalation), the conversation appears in `/operator`'s inbox with
its brief, I reply from `/operator` and see it land in `/chat`, click
**Resumir** and the AI answers the next `/chat` message again; plus
**Assumir** from the Ativas tab moves an AI conversation into the inbox;
plus tests covering the list filtering/ordering the page relies on.
