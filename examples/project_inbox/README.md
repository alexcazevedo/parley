# Project inbox: an example of the `external` channel

A project with its own inbox (about 290 lines, `app.py`) that forwards
messages to parley, receives the agent's replies as signed pushes, and catches
up from the feed after a restart or a given-up event. It imports nothing from parley.

## Run it

1. Start parley with the stub model and this example:

   ```
   docker compose -f docker-compose.yml -f docker-compose.stub.yml -f docker-compose.example.yml up -d --build
   ```

2. Create an `external` channel (admin and tenant steps as in the other guides).
   Use exactly this `events_url`: the host `project-inbox` is the compose
   service, and `docker-compose.example.yml` lists it in `CHANNEL_ALLOWED_HOSTS`
   for the api and the worker (without that, the private host is refused):

   ```
   POST /channels
   {"type": "external", "name": "Inbox", "agent_id": "<agent id>",
    "config": {"events_url": "http://project-inbox:9200/parley/events"}}
   ```

   The answer holds the channel `id` and `events_secret` (shown once).

3. Give the service its variables and recreate it:

   ```
   export PARLEY_API_KEY=<tenant api key> PARLEY_CHANNEL_ID=<channel id> PARLEY_EVENTS_SECRET=<events_secret>
   docker compose -f docker-compose.yml -f docker-compose.stub.yml -f docker-compose.example.yml up -d project-inbox
   ```

   The variables live only in that shell: a later `up` from a shell without
   the three exported recreates `project-inbox` unconfigured. Put them in `.env`
   to avoid that. The inbox is in the container's `/tmp` (see
   `docker-compose.example.yml`): a restart keeps it, removing the container
   loses it.

   Variables: `PARLEY_URL` (set to `http://api:8000`), `PARLEY_API_KEY`,
   `PARLEY_CHANNEL_ID`, `PARLEY_EVENTS_SECRET`, `INBOX_STATE_PATH` (set to
   `/tmp/inbox.json`), `CATCH_UP_SECONDS` (default 30: how often it reads the feed).

4. Talk to it on port 9200:

   ```
   curl -X POST localhost:9200/customer -H 'content-type: application/json' \
     -d '{"user_id": "ana", "text": "Where is my order?"}'
   curl localhost:9200/inbox
   ```

   Open `http://localhost:9200/` for the same data as a page.

## Routes

- `POST /customer` `{user_id, text}`; `POST /staff` `{user_id, text, name}` (pauses the agent).
- `POST /hand-back` `{user_id}`: gives the conversation back to the agent.
- `POST /parley/events`: where parley pushes; 401 on a bad signature.
- `POST /catch-up`: reads the feed from the stored `seq` (also done at start and every `CATCH_UP_SECONDS`).
- There is no media route: send media to parley directly (`POST /channels/external/media`). A message sent straight to parley shows in this inbox only as the agent's reply.
- `GET /inbox`: per customer, `messages`, `agent_active`, `department`, `handoff_brief`.

State lives in a JSON file, so a restart keeps the inbox, the feed position and the applied event ids (newest 1000). The position `seq` moves only when the feed is read, never on a push: after parley gives up on an event it pushes later ones, and a position moved by them would skip it. A push is applied and its id remembered; a feed read skips ids already applied.
