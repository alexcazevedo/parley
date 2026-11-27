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

A call with unknown outcome is visible only in the logs and in `tool_executions` rows still `started`. A tool from an MCP server is repeated only when the server marks it read-only; each row also holds the `call_id` and `context` sent with the call ([business-tools](business-tools.md) step 7).

### Short retry on 429 and 5xx

Model calls, speech-to-text and `GET` tools are retried when the answer is 429, 500, 502, 503 or 504: up to `HTTP_RETRY_ATTEMPTS` tries in total, waiting `HTTP_RETRY_BASE_SECONDS` and doubling, up to `HTTP_RETRY_MAX_WAIT_SECONDS`; a numeric `Retry-After` sets the wait (log event `http.retry`). Non-`GET` tools and sends to the channel are never retried at this level, and nothing is retried after a timeout or a connection error, because the request may have been processed.

On `openai`, `anthropic` and `google` there is one retry rule: the vendor's own client retries, `HTTP_RETRY_ATTEMPTS - 1` times, with the vendor's own waits, and parley adds nothing on top (no `http.retry` log events for them). It differs from `openai_compatible`, where parley retries 429 and 5xx itself as above. The turn deadline (60 seconds, fixed in the code) is the same on every provider and bounds the retries.

### The worker keeps running

An error in the job loop (Redis down or restarting, a job that cannot be handled) is logged as `worker.job_loop_error`; the loop waits 1 second and tries again. The housekeeping pass and the scheduler loop also log their errors and go on.

The worker also discovers MCP servers' tools: every `MCP_SYNC_INTERVAL_SECONDS` it syncs the servers not synced since then, at most 20 a pass; a failure is logged as `worker.mcp_sync_error` and the server's `status` becomes `error` ([business-tools](business-tools.md) step 10). The worker also ends sessions: every `SESSION_END_INTERVAL_SECONDS` it ends the open sessions silent past their agent's `session_gap_hours` and writes the recap of each ended session ([agent-behaviour](agent-behaviour.md) section 2); an error is logged as `worker.session_end_error`. The worker also runs the knowledge ingest loop. Every `KNOWLEDGE_INGEST_INTERVAL_SECONDS` it takes the knowledge items that are `processing` (new or edited), reads documents, splits the text into passages and sets the item `ready` or `failed` ([knowledge](knowledge.md)). An error is logged as `worker.knowledge_ingest_error` and the next pass goes on. An item is claimed for 120 seconds, so a worker that dies leaves it to be taken again after that. With no worker running, items stay `processing` and are not searchable. If embeddings are on, the pass also embeds the new passages; a failure there is logged (`knowledge.embed_failed`) and leaves the item `ready` with keyword search. After you change `EMBEDDINGS_MODEL`, or start using embeddings on a base that has passages, run `uv run python -m app.knowledge.reindex` (`docker compose run --rm api python -m app.knowledge.reindex`; `--all` embeds every passage again). It prints `embedded N passages`, and exits with 1 when embeddings are off or the Postgres `vector` extension is missing (the compose image `pgvector/pgvector:pg16` has it).

One worker runs up to `WORKER_CONCURRENCY` conversations at once, never the same conversation twice. A turn holds a database connection only while it reads or writes: it commits before every model call, tool call, speech-to-text call, send and paced wait. The exception is the API's request paths, which hold a transaction while they fetch media. The engine uses SQLAlchemy's default pool (5 connections, up to 10 more), so keep `WORKER_CONCURRENCY` well under that.

### Shutdown

On `SIGTERM` or `SIGINT` the worker takes no new job and no new turn, and waits up to 90 seconds for the running turns. After that it cancels what is left: those turns release their lock, their uncommitted work is rolled back, and their messages come back after the claim timeout. A cancelled turn may resend bubbles it already sent. `docker-compose.yml` sets `stop_grace_period: 100s` on `worker` (Docker's default is 10 seconds) and `restart: unless-stopped`. Log events: `worker.stopping`, `scheduler.stopping`, `scheduler.stopped`, `worker.shutdown_grace_expired`, `scheduler.turns_cancelled`.

### Limits

- Rows a turn wrote before a slow call stay when the turn later fails. A retried turn's prompt contains the earlier attempt's saved tool messages.
- A reply row is stored after its send, so a crash between the two repeats the reply. A handoff the tool committed, followed by a crash before the handoff message is sent, leaves the customer without that message.
- The error message after the last attempt is stored first and then sent best effort; on a broken channel the customer may not receive it.
- The reliability behaviour is tested with fakeredis and `respx` only, not against a real Redis, model or channel.

Check: stop the worker with `docker compose stop worker`; the log shows `worker.stopping` and the container exits within the grace period.

## A model per job

Settings and values are in [configuration](configuration.md), what each job does in [agent behaviour](agent-behaviour.md) section 8. Every call of the jobs writes one row to `model_job_calls`, after the call and in its own transaction; a failure to write it is logged (`model_job.record_failed`) and never fails the job. Columns: `tenant_id`, `conversation_id`, `agent_run_id` (set only for the boundary check, and not a foreign key), `job` (`boundary_check`, `sentiment`, `category`, `summary`, `handoff_brief`, `session_recap`), `model` (`typesafe:<model>` for a decision model, otherwise the chat model's name), `outcome` (`ok`, `error`, `timeout`, `unusable`), `handed_over`, `prompt_tokens`, `completion_tokens`, `latency_ms`, `created_at`. Tokens are empty when the service reported none. The table holds token counts, not a price: TypeSafe reports input and output tokens and no cost, so work the cost out from its price list. In a live check against TypeSafe (model `jev-1.14.0`, 8 calls on made-up examples) each call took about 220 to 450 ms and none failed; that is a sample, not a promise.

Per job, over the last 7 days:

```
docker compose exec postgres psql -U harness -d harness -c "
SELECT job, model,
       count(*) AS calls,
       count(*) FILTER (WHERE handed_over) AS handed_over,
       count(*) FILTER (WHERE outcome <> 'ok') AS not_ok,
       sum(prompt_tokens) AS prompt_tokens,
       sum(completion_tokens) AS completion_tokens,
       round(avg(latency_ms)) AS avg_ms,
       percentile_cont(0.95) WITHIN GROUP (ORDER BY latency_ms) AS p95_ms
FROM model_job_calls
WHERE created_at > now() - interval '7 days'
GROUP BY job, model ORDER BY job, calls DESC;"
```

Add `AND job = 'boundary_check'` for one job. A `handed_over` count that keeps rising means the decision model cannot take that job's calls (too many departments or rules, or images): the job is on the agent's model anyway. `not_ok` is every call that errored, timed out or gave an answer the job could not use; on the boundary check each of those blocked a reply, because it fails closed.

### Session recaps

Each ended session is a row of `conversation_sessions` (`status` `open`, `pending`, `done` or `failed`; `ended_by` `silence` or `closed`). It makes one `session_recap` call, recorded in `model_job_calls` like the others, so the cost is one call per ended session, plus up to two repeats when one fails. A session with no customer message makes none. Rows waiting for their recap, and rows that gave up:

```
docker compose exec postgres psql -U harness -d harness -c "
SELECT status, ended_by, ended_at, error
FROM conversation_sessions
WHERE status IN ('pending', 'failed') ORDER BY ended_at;"
```

A `pending` row is normal for a minute or so. One that stays means no worker is running, or its agent has `session_memory_enabled` off (the worker skips those). After a failed try `error` reads `1/3: reason`; the third makes the row `failed` and the session has no recap. Nothing retries a `failed` row; set its `status` back to `pending` and clear `error` to try again.

### Compare before you switch

A decision model's answers are not the same as the agent model's, and three examples are not a measurement. In the live check, the first version of the `allowed` question did not state the agent's rules, and an out-of-scope reply scored 0.48 and then 0.52, so it was allowed. That was fixed, and it is why you compare on your own replies before you trust a model.

```
uv run python -m app.llm.compare --tenant <tenant_id> --model typesafe:<model> [--limit 200]
docker compose run --rm api python -m app.llm.compare --tenant <tenant_id> --model llm:<model>
```

It takes the tenant's newest successful agent runs (up to `--limit`, default 200) of agents with boundaries and the reply check on, judges each stored reply again on the candidate, and prints each run where the candidate disagrees with the recorded result (blocked or allowed), then a line `compared N replies: A agree, D disagree, S skipped`. Limits:

- Boundary check only. Sentiment and category cannot be compared yet.
- It uses the agents' current rules, not the ones in force when the reply was written, so a rule you changed since changes the answer.
- It prints customer text: a preview of each disagreeing reply, 120 characters. Run it where that is allowed.
- It sends the stored replies and user messages of those runs to the candidate model. A `typesafe:` candidate needs `TYPESAFE_API_KEY`. It writes nothing, and records nothing in `model_job_calls`.
- Skipped: runs with no reply, runs recorded as `check_failed` or `check_unparseable`, and calls the candidate failed, timed out, handed over or could not read. A candidate block with no rule named (`no_rule_identified`) counts as a real block.
- Exit 0 when it ran; 1 for a bad argument or an unknown tenant.

The recorded results come from the check that ran then, so a disagreement is not always the candidate's fault: read the replies it prints. Look at both kinds: a reply the candidate allows that was blocked (a cheaper model that lets more through), and a reply it blocks that was allowed (more refusals for good replies).

### Recommended order

1. Run the comparison on the job's candidate model and read every disagreement.
2. Set one job (for example `BOUNDARY_CHECK_MODEL`), restart `api` and `worker`. Only the decision jobs can use `typesafe:`; set `TYPESAFE_API_KEY` first or both refuse to start.
3. Watch `model_job_calls` for a few days: `not_ok`, `handed_over`, latency, tokens. Watch `agent_runs.boundary_reason` for `check_failed` and `no_rule_identified`, and the refusals customers get.
4. Set the next job. To go back, empty the setting and restart; rows stay.

`scripts/demo_decision_model.py` is a live check of the decision model, not part of the test suite: `uv run python scripts/demo_decision_model.py` with `TYPESAFE_API_KEY` set sends a few made-up examples (no real customer text) and prints what it decided; for the boundary examples it also prints the probability of "allowed". It exits 0 with `decision model demo: ok` when every example is decided as expected, 1 on an unexpected answer or a service error, 2 when there is no key. A few examples are a smoke test, not a measure of quality.

### Native providers

`agent_runs` has `provider` and `model` columns (migration `0030`): the provider and model that answered the run, null on older rows. Nothing changes for an existing deployment: agents stay on `openai_compatible`, and the migration only adds the columns.

No real vendor was called while this was built, because no key was available. What is not verified: which image formats each vendor accepts, how each reports token use, whether a tool call id from one vendor is accepted after a switch to another, and whether OpenAI's strict mode accepts the schemas of the pick-one decisions. Before you put an agent on a new provider, run `uv run python scripts/demo_native_providers.py` with the vendor's key in `.env` or the environment, optionally with `--openai-model`, `--anthropic-model` or `--google-model`. For each provider whose key is set it sends a plain reply, a tool call and a typed boundary decision, with made-up examples, and prints the latency and token use; the keys are not printed. It exits 0 with `native providers demo: ok`, 1 on a failure, and 2 with `no provider key is set: live check not done` when no key is set. It is a smoke test, not part of the test suite.

Check: after one message on an agent with `BOUNDARY_CHECK_MODEL` set, the query above shows a `boundary_check` row with that model.
