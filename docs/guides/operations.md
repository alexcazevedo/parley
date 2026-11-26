# Operations

Run parley beyond a first try: more than one API replica, the image user, upgrading, and what happens to a customer's message when a worker dies or a turn fails. Start from a healthy stack ([quickstart](quickstart.md)). Settings are in [configuration](configuration.md). Run commands from the repository root.

## Stack notes

- Compose publishes only the API, on `API_PORT` (default 8001). `postgres` and `redis` have no host ports unless you add `docker-compose.dev.yml` (see the quickstart).
- Two API replicas cannot share one host port, so give `API_PORT` a range:

```
API_PORT=8001-8002 docker compose up -d --scale api=2
```

Each replica answers `GET /ready` on its own port. The API does not migrate at start, so replicas do not race on the schema.

- The image runs as the user `app` (uid and gid 10001), not root. `/srv` and `/srv/media` belong to `app`. A new `media_data` volume is writable by it. A volume created by an older image that ran as root stays owned by root, and `app` cannot write to it. Change the owner once:

```
docker compose run --rm --user root --entrypoint chown api -R 10001:10001 /srv/media
```

- Logs are JSON lines. While a turn runs, every line carries `conversation_id` and, once the conversation has loaded, `tenant_id`; lines from the agent run also carry `run_id`. API lines carry `request_id`. To follow one conversation, filter the worker's output on its id:

```
docker compose logs worker | grep '<conversation_id>'
```

Check: `docker compose ps api` lists the replicas, and `GET /ready` answers 200 on each port.

## After upgrading

`docker-compose.yml` no longer sets `TOOL_ALLOWED_HOSTS` to `mock-tools` for `api` and `worker`; both take the value from `.env`. With it empty (the `.env.example` default) public tool hosts pass and private, loopback and link-local ones are refused. If you used the bundled `mock-tools` service, add `TOOL_ALLOWED_HOSTS=mock-tools` to `.env` and run `docker compose up -d`; the list is then exclusive.

Older versions allowed a channel or a conversation to point at another tenant's agent, and an Evolution channel with a private `base_url`. Such a conversation now gets no turn and no summary. A channel with a private `base_url` stops sending until its host is listed in `CHANNEL_ALLOWED_HOSTS` for both `api` and `worker`.

List the affected rows once after upgrading:

```
docker compose run --rm api python -m app.audit
docker compose run --rm api python -m app.audit --resolve
```

The plain command checks `base_url` addresses without DNS, so a private host name such as `http://evolution:8080` is not listed by it. `--resolve` looks each host name up and also lists channels that resolve to a private address or do not resolve; run it where the server runs, so it uses the same DNS.

It prints one line per finding, `<kind> <row_id> tenant=<tenant_id> <detail>`, then `N offending rows`. The exit code is 1 when N is above 0, otherwise 0. The kinds:

- `channel_foreign_agent`: a channel whose `agent_id` belongs to another tenant (`detail` is `agent=<id>`).
- `conversation_foreign_agent`: a conversation whose `agent_id` belongs to another tenant (same `detail`).
- `channel_refused_base_url`: an Evolution channel whose `base_url` the check refuses (`detail` is the reason). Listing the host in `CHANNEL_ALLOWED_HOSTS` removes it from the report.

The command only reads, prints no secrets, and without `--resolve` makes no network or DNS call. Fixing the rows is up to you.

Check: the last line reads `0 offending rows` and the exit code is 0.

## Reliability

Delivery is at-least-once, not exactly-once. A worker that dies after it sent a reply and before it marked the turn done repeats that reply after recovery (about 60 seconds later). A send that raised an error but did reach the customer is retried and can repeat.

### A worker dies

- Before the message is in the debounce batch: a worker moves a job from `harness:jobs` to its own list `harness:processing:{worker_id}` and removes it only after the message is in the batch. Each worker refreshes an alive key (`harness:worker:{id}`, 30 second TTL) and runs a housekeeping pass every 10 seconds. A pass puts the jobs of a worker whose key expired back on `harness:jobs` (log event `worker.jobs_reaped`). A message that reaches the batch twice is answered once.
- After a conversation was claimed: the scheduler takes a claim (`harness:claim:{conversation_id}`, set only if absent, 60 seconds) and moves the due time 60 seconds out. The messages being answered wait in `harness:inflight:{conversation_id}` until the turn is done. If the worker dies, the conversation is due again after 60 seconds and the next turn answers the same messages.
- During a long turn: the conversation lock (60 seconds) and the claim are renewed every 20 seconds, so a long turn never runs twice in parallel. A turn that finds the lock is no longer its own stops before it sends or saves anything else.

### A turn fails

Anything that escapes the turn (a send that fails, a database error, a speech-to-text failure) puts the messages back for another attempt:

- Up to 3 attempts: the retry comes 2 seconds after the first failure and 8 seconds after the second, counted from the moment the turn failed. Messages that arrive meanwhile join the next attempt. Log event `scheduler.turn_retry`.
- After the third failure the batch goes to the dead-letter list `harness:dlq:turns` and the customer gets one error message: the agent's `error_message`, else `Desculpe, tive um problema técnico. Pode tentar novamente?`. Log event `scheduler.turn_dead_lettered`.
- If part of a reply was already delivered (the first bubble went out, the second failed), there is no retry: what was sent stays, the rest is dropped, the batch is dead-lettered with `partly_delivered` true and no error message is added.

Read the dead-letter list with Redis:

```
docker compose exec redis redis-cli LRANGE harness:dlq:turns 0 -1
```

Each entry is a JSON object: `conversation_id`, `message_ids`, `error` (`"Type: message"`), `partly_delivered`, `attempts` and `failed_at` (unix seconds), oldest first. Nothing reads the list or removes entries: there is no API for it and no replay. `harness:dlq` is a different list, the job queue's own; the worker does not use it today.

Model errors that remain after the short retry below are answered with the agent's `error_message` (or its default text) as a normal reply, and the turn is not run again for them.

### Tool calls are recorded first

Before a tool runs, its call (name and arguments) is saved in `tool_executions` with status `started`, in its own transaction; when the tool returns, the row is completed with the result or the error. When a turn runs again for the same messages (a retry, or a turn merged with newer messages), a call with the same tool and arguments as an earlier attempt is not made again:

- If the earlier call returned, the model gets the saved result, an error included (log event `tools.result_reused`). This holds only within re-runs of the same turn, never in a later turn.
- If the earlier call was started and has no result, an HTTP tool with method `GET` is called again. Any other HTTP tool is not: the model is told the outcome is unknown and the call was not repeated, and `tools.outcome_unknown` is logged as a warning. A call that never left (connection refused, connect timeout, blocked endpoint, invalid arguments) or that got an HTTP response of any status has a saved result.
- The built-in tools (`remember`, `handoff_to_human`) save their effect together with their result, so a call with no result did nothing and runs again.

A call with unknown outcome is visible only in the logs and in `tool_executions` rows still `started`.

### Short retry on 429 and 5xx

Model calls, speech-to-text and `GET` tools are retried when the answer is 429, 500, 502, 503 or 504: up to `HTTP_RETRY_ATTEMPTS` tries in total, waiting `HTTP_RETRY_BASE_SECONDS` and doubling, up to `HTTP_RETRY_MAX_WAIT_SECONDS`; a numeric `Retry-After` sets the wait (log event `http.retry`). Non-`GET` tools and sends to the channel are never retried at this level, and nothing is retried after a timeout or a connection error, because the request may have been processed.

### The worker keeps running

An error in the job loop (Redis down or restarting, a job that cannot be handled) is logged as `worker.job_loop_error`; the loop waits 1 second and tries again. The housekeeping pass and the scheduler loop also log their errors and go on.

The worker also runs the knowledge ingest loop. Every `KNOWLEDGE_INGEST_INTERVAL_SECONDS` it takes the knowledge items that are `processing` (new or edited), reads documents, splits the text into passages and sets the item `ready` or `failed` ([knowledge](knowledge.md)). An error is logged as `worker.knowledge_ingest_error` and the next pass goes on. An item is claimed for 120 seconds, so a worker that dies leaves it to be taken again after that. With no worker running, items stay `processing` and are not searchable. If embeddings are on, the pass also embeds the new passages; a failure there is logged (`knowledge.embed_failed`) and leaves the item `ready` with keyword search. After you change `EMBEDDINGS_MODEL`, or start using embeddings on a base that has passages, run `uv run python -m app.knowledge.reindex` (`docker compose run --rm api python -m app.knowledge.reindex`; `--all` embeds every passage again). It prints `embedded N passages`, and exits with 1 when embeddings are off or the Postgres `vector` extension is missing (the compose image `pgvector/pgvector:pg16` has it).

One worker runs up to `WORKER_CONCURRENCY` conversations at once, never the same conversation twice. A turn holds a database connection only while it reads or writes: it commits before every model call, tool call, speech-to-text call, send and paced wait. The exception is the API's request paths, which hold a transaction while they fetch media. The engine uses SQLAlchemy's default pool (5 connections, up to 10 more), so keep `WORKER_CONCURRENCY` well under that.

### Shutdown

On `SIGTERM` or `SIGINT` the worker takes no new job and no new turn, and waits up to 90 seconds for the running turns. After that it cancels what is left: those turns release their lock, their uncommitted work is rolled back, and their messages come back after the claim timeout. A cancelled turn may resend bubbles it already sent. `docker-compose.yml` sets `stop_grace_period: 100s` on `worker` (Docker's default is 10 seconds) and `restart: unless-stopped`. Log events: `worker.stopping`, `scheduler.stopping`, `scheduler.stopped`, `worker.shutdown_grace_expired`, `scheduler.turns_cancelled`.

### Limits

- Rows a turn wrote before a slow call stay when the turn later fails. A retried turn's prompt contains the earlier attempt's saved tool messages.
- A reply row is stored after its send, so a crash between the two repeats the reply. A handoff the tool committed, followed by a crash before the handoff message is sent, leaves the customer without that message.
- The error message after the last attempt is stored first and then sent best effort; on a broken channel the customer may not receive it.
- The reliability behaviour is tested with fakeredis and `respx` only, not against a real Redis, model or channel.

Check: stop the worker with `docker compose stop worker`; the log shows `worker.stopping` and the container exits within the grace period.
