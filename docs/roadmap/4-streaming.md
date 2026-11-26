# Brief 4: streaming replies on web chat

Mode: `mvp`. Routing profile `cloud`, sequential. Needs brief 3 done. Build it only when a channel needs it: it is the least valuable of the four and the one most in tension with how parley protects a reply.

## The problem

On web chat a customer waits for the whole reply before seeing anything. A long answer feels slow even when the model started writing at once.

## Why this is not simple in parley

- **A reply is checked before the customer sees it.** The boundary check (`app/agents/boundaries.py`) reads the finished reply and can replace it with a refusal. Text already shown cannot be taken back.
- **Most channels cannot show partial text.** WhatsApp through Evolution and the external channel deliver whole messages; the external channel's pushes are signed events.
- **Replies are shaped after they are written.** The humanizer (`app/conversations/humanize.py`) splits a reply into parts and paces them.
- **A turn can be dropped.** A newer customer message supersedes the unsent reply (barge-in, `app/conversations/turn.py`).
- **The worker writes the reply, the API serves the customer.** They are separate processes; today web chat polls.

## Decided

**Web chat only.** Every other channel keeps whole messages. Nothing changes for them.

**Off by default, switched per agent.** Existing agents behave as before.

**The boundary check is never weakened.** An agent whose reply check is on does not stream text the check has not passed. What it can show early is that the agent is writing, and the reply once checked. Streaming of unchecked text is available only to an agent whose reply check is off, and the guide says plainly what that gives up.

**Partial text is never the record.** The stored message is the final reply, written once, as today. A page that reloads, a client that never opened a stream, and the operator screen all see the same final message. Polling keeps working unchanged.

**A dropped turn ends its stream.** When a turn is superseded or fails, the client is told to discard what it showed.

**Tool steps are not streamed.** Only the final reply is. While tools run, the client sees that the agent is working.

**The humanizer and streaming do not combine.** A streamed reply is one message.

## Open for the grill

1. Whether the milestone is still wanted once briefs 1 to 3 are in use. Recommended: ask for the channel and the customer that need it before planning.
2. Server-sent events or a WebSocket from the API to the browser. Recommended: server-sent events, which fit the one-way flow and plain HTTP.
3. How partial text travels from the worker to the API process. Recommended: Redis, which both already use, with a short lifetime.
4. For an agent with the reply check on: show only "writing" and then the checked reply, or check the text in chunks as it grows. Recommended: the first; chunked checking costs a model call per chunk and can still pass text that the whole reply would fail.

## Read before planning

- The current Pydantic AI documentation on streaming a single model request, and how tool calls appear in a stream.
- In this repository: `app/agents/runtime.py` (`_loop`, `_enforce_boundaries`), `app/agents/boundaries.py`, `app/conversations/turn.py` (delivery, barge-in, the superseded status), `app/conversations/humanize.py`, `app/api/webchat.py`, `app/channels/webchat.py`, `app/static/chat.html`, `docs/guides/messaging.md`.

## Tests

A streamed reply ends as the same stored message a non-streamed turn would store. With the reply check on, no unchecked text reaches the client and a blocked reply shows only the refusal. A superseded turn tells the client to discard. A turn with a tool call streams only the final reply. A client that only polls sees no difference. Another channel type with the switch on behaves as before. No network.

## Out of scope

Streaming on WhatsApp or the external channel. Streaming tool calls or reasoning. Voice. Streaming to the operator screen. Any weakening of the boundary check.

## Done when

On web chat, with the switch on for an agent whose reply check is off, the customer sees the reply grow as it is written and the stored message equals the final text; with the reply check on, the customer sees that the agent is writing and then the checked reply, and a reply that breaks a boundary shows only the refusal; a newer customer message during a stream makes the page discard the partial text; a client that only polls and every other channel behave exactly as before.
