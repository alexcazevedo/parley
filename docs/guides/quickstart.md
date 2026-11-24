# Quickstart

Bring up the stack with Docker Compose and confirm it is healthy. Run every command from the repository root.

## Prerequisites

- Docker with the Compose plugin.
- `curl` and `jq` for the commands in these guides.
- An OpenAI-compatible model key for real conversations. The stack starts without a working one.

## 1. Create `.env`

```
cp .env.example .env
```

Edit `.env` and set:

- `ADMIN_API_KEY`: a long random string. Not the placeholder.
- `SECRETS_KEY`: a Fernet key. Generate one with `openssl rand -base64 32 | tr '+/' '-_'` (no Python needed; the alternative and a way to check the key are in [configuration](configuration.md)).
- `LLM_API_KEY`: your model provider key. Also check `LLM_BASE_URL` and `LLM_MODEL`.

Every other variable has a working default. The full list is in [configuration](configuration.md).

Check: `grep -c changeme .env` prints `0` once `ADMIN_API_KEY`, `SECRETS_KEY` and `LLM_API_KEY` are all set (the `.env.example` placeholders of all three contain `changeme`). Without a model key it prints `1`; that is fine for [the stub](#try-it-without-a-model-key). `SECRETS_KEY` is set when `grep -c '^SECRETS_KEY=.\{40,\}' .env` prints `1`.

## 2. Start the stack

```
docker compose up -d --build
```

With no model key, start the stub instead of this command: `docker compose -f docker-compose.yml -f docker-compose.stub.yml up -d --build` (see [the stub](#try-it-without-a-model-key)). Every later step is unchanged, and `docker compose ps` then also lists `mock-llm`. Compose reads only `docker-compose.yml` unless told otherwise, so keep passing both `-f` flags for `up`, `build` and `down`: `up` with one file recreates `api` and `worker` with the model settings of `.env`, and `down` with one file leaves `mock-llm` running. Commands that name `mock-llm` (`ps mock-llm`, `logs mock-llm`) need both flags too, since the service is unknown without the stub file. `docker compose ps`, `ps worker` and `logs api worker` work with either form.

Services start in this order:

1. `postgres` and `redis` start and become healthy.
2. `migrate` runs `alembic upgrade head` once and exits with code 0.
3. `api` and `worker` start only after `migrate` succeeded. If `migrate` fails neither starts.
4. `mock-tools` is a small demo business endpoint on the compose network. `api` waits for it.

Check: `docker compose ps -a` shows `postgres`, `redis`, `api`, `worker` and `mock-tools` as running or healthy, and `migrate` as `Exited (0)`. If `api` or `worker` exited, run `docker compose logs api worker`. A message naming `ADMIN_API_KEY` or `SECRETS_KEY` means that variable is unset or still a placeholder.

## 3. Check health

The API listens on host port `API_PORT` (default 8001). To use another port, add `API_PORT=<port>` to `.env` (compose reads `.env` for this) before step 2, and use that port in `BASE_URL` below.

```
BASE_URL=http://localhost:8001
curl -s $BASE_URL/health
curl -s -w '\n%{http_code}\n' $BASE_URL/ready
```

- `GET /health` is liveness. It does not touch Postgres or Redis. Expected: 200 and `{"status":"ok"}`.
- `GET /ready` checks Postgres and Redis, each with a 2 second limit. Expected: 200 and `{"status":"ready","checks":{"database":"ok","redis":"ok"}}`. When one fails the status is 503, the body says `"status":"not_ready"` and the failed check reads `"failed"`.

Neither endpoint checks the worker, the model endpoint or the media disk. Check the worker with `docker compose ps worker`. A failed check is logged as `ready.check_failed` with the check name and the error.

Check: `GET /ready` returns 200. If the API was only just started, retry for up to 30 seconds.

## 4. Optional: demo mode

Set `DEMO_MODE=true` in `.env` and run `docker compose up -d` again to try parley with no setup. It skips the `ADMIN_API_KEY` and `SECRETS_KEY` checks and also mounts:

- `GET /chat`: a built-in chat page.
- `POST /chat/bootstrap`: no authentication; creates a demo tenant, agent and webchat channel, and returns `api_key`, `channel_id` and `agent_id`.
- `POST /dev/messages`: a message simulator.

`GET /operator`, the operator inbox page, is mounted in every mode, not only in demo mode. Paste a tenant key into it; the key is prefilled only in demo mode.

The `/chat` page holds the tenant key in the browser, so it is a demo, not a widget for real customers: send their messages from your own server with the tenant key. Both pages show images as thumbnails and audio as a player with the transcript. `/chat` has buttons to attach a photo or audio file and to record a voice note.

With a model key in `.env`, two scripts drive the stack over HTTP: `uv run scripts/demo.py` (a burst of messages, a tool call, a handoff, an operator reply, resume) and `uv run scripts/demo_media.py` (an image and a voice note in one turn). Both exit 0 on success, 2 when `LLM_API_KEY` is still the placeholder and 1 when an expectation was not met.

Never use demo mode on a server others can reach. With a placeholder `SECRETS_KEY`, channels with a `secret` and tools with `auth_config` fail with 500.

Check: `curl -s -X POST $BASE_URL/chat/bootstrap` returns JSON with `api_key`, `channel_id` and `agent_id`. Without demo mode it returns 404.

## Try it without a model key

To run the stack with no model key, add the stub file:

```
docker compose -f docker-compose.yml -f docker-compose.stub.yml up -d --build
```

This adds a `mock-llm` service and points `api` and `worker` at it with `LLM_BASE_URL`, `LLM_API_KEY` and `LLM_MODEL`, which override `.env`. The stub answers in place of the model:

- A customer message gets the reply `Stub reply to: <the message>`.
- When the agent has the `handoff_to_human` tool and the message contains `human`, `person`, `pessoa` or `atendente`, the stub calls that tool and the conversation is handed off.
- The reply check, the sentiment score, categorization, the rolling summary and the handoff brief get fixed answers: the reply is allowed, the customer is calm, no category fits, and the summaries are one fixed sentence.

The stub does not reason, call your tools (other than `handoff_to_human`), read images, transcribe audio or stream. It ignores the agent's own `model` and prompt. Use it to check that the stack and your integration work, not to judge answers.

Check: `docker compose -f docker-compose.yml -f docker-compose.stub.yml ps mock-llm` shows it running.

## Reaching Postgres and Redis from the host

Compose publishes no database ports by default. For work from the host (tests, `uv run`) add the dev file:

```
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

Postgres is then on host port `POSTGRES_PORT` (default 5434) and Redis on `REDIS_PORT` (default 6380). Do not use the dev file on a server. `make db` starts only Postgres this way, and creates `.env` first if it is missing.

Check: `docker compose -f docker-compose.yml -f docker-compose.dev.yml ps postgres` lists a `0.0.0.0:5434->5432/tcp` port.

## Stopping

```
docker compose down
```

This keeps the data in the `postgres_data` and `media_data` volumes. `docker compose down -v` deletes them.

Check: `docker compose ps -a` lists nothing.

## Next

Create a tenant, an agent, a tool and a channel in [setup](setup.md).
