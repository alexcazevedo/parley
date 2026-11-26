# Configuration

Parley reads its settings from environment variables. It also reads a `.env` file in its working directory. Copy `.env.example` to `.env` to start. A variable name is the setting name in upper case.

The API and the worker read the same settings. Set them for both.

## Required to boot

A non-demo start refuses to run unless these are set to real values.

- `ADMIN_API_KEY`: the shared key sent as `X-Admin-Key` to create tenants. Default: the placeholder `changeme-admin-key`. The process refuses to start if it is empty or still the placeholder. Set a long random string.
- `SECRETS_KEY`: the Fernet key that encrypts channel secrets and tool credentials at rest. Default: a placeholder that is not a valid key. The process refuses to start if it is not a valid Fernet key. Generate one with:

```
openssl rand -base64 32 | tr '+/' '-_'
```

This prints 44 characters: 32 random bytes as URL-safe base64, which is a valid Fernet key. It needs only `openssl` and `tr`. To check a key, `uv run python -c "from cryptography.fernet import Fernet; import sys; Fernet(sys.argv[1].encode())" "<the key>"` exits 0 when it is valid. Alternative, with `python3` and the `cryptography` package installed:

```
python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Keep `SECRETS_KEY` stable. Changing it makes stored secrets unreadable.

- `DEMO_MODE`: default `false`. When `true`, the two checks above are skipped and the demo surface is mounted. Use it only on a local or throwaway stack. A placeholder `SECRETS_KEY` cannot encrypt, so a channel with a `secret` or a tool with `auth_config` fails with 500 in demo mode until you set a real key.

You must also set `LLM_API_KEY` for any real model call. It is not checked at start.

## Connections

- `DATABASE_URL`: Postgres connection, SQLAlchemy async with the asyncpg driver. Default: `postgresql+asyncpg://` with the user, password and database `harness` on host `postgres`, port 5432. Change it to point at your database. Docker compose overrides it inside the containers.
- `REDIS_URL`: Redis connection for the queue, locks and debounce. Default: `redis://redis:6379/0`. Change it to point at your Redis.
- `LOG_LEVEL`: log level, one of `DEBUG`, `INFO`, `WARNING`, `ERROR`. Default: `INFO`. Raise it to `DEBUG` when diagnosing.

## Model

Parley calls any OpenAI-compatible chat-completions endpoint.

- `LLM_BASE_URL`: base URL of the provider. Default: `https://api.openai.com/v1`. Change it for another provider or a local server.
- `LLM_API_KEY`: key for the provider. Default: the placeholder `changeme-llm-key`. Set your real key.
- `LLM_MODEL`: default model name. Default: `gpt-4o-mini`. Change it to the model you use.
- `LLM_MAX_TOKENS_PARAM`: name of the token-limit field sent to the model. Default: `max_tokens`. Set `max_completion_tokens` for models that reject `max_tokens`. It applies to the whole deployment.
- `HTTP_RETRY_ATTEMPTS`: tries for a model call, a speech-to-text call or a GET tool call that gets 429 or 5xx. The first try counts. Default: `3`. Minimum 1. Set `1` to turn retries off.
- `HTTP_RETRY_BASE_SECONDS`: first wait between tries. It doubles each time. A numeric `Retry-After` header replaces it. Default: `0.5`.
- `HTTP_RETRY_MAX_WAIT_SECONDS`: longest single wait. Default: `10.0`.

## Speech-to-text and vision

- `STT_BASE_URL`: base URL for voice-note transcription. Default: unset, so `LLM_BASE_URL` is used. Set it to use a different transcription backend.
- `STT_API_KEY`: key for transcription. Default: unset, so `LLM_API_KEY` is used.
- `STT_MODEL`: transcription model. Default: `whisper-1`.
- `STT_LANGUAGE`: language hint sent with the audio. Default: `pt`. Set it to your customers' language, or empty to send no hint.
- `STT_TIMEOUT_SECONDS`: time allowed for one transcription. Default: `20.0`.
- `LLM_VISION_ENABLED`: send customer images to the model. Default: `true`. Set `false` if your model cannot read images. Images then reach the model as the placeholder text.
- `VISION_MAX_IMAGES`: how many of the newest images in the context window are sent as real image data. Older ones become text. Default: `3`. Lower it to save tokens.
- `IMAGE_PLACEHOLDER`: text the model reads in place of an image it cannot see. Default: `[imagem enviada pelo cliente]`. Change it to your customers' language. It is never sent to a customer.
- `AUDIO_PLACEHOLDER`: text the model reads in place of a voice note it cannot hear. Default: `[áudio enviado pelo cliente]`. It is never sent to a customer.
- `PDF_MAX_PAGES`: pages read from a PDF sent on an external channel. Default: `30`. Minimum 1.
- `PDF_MAX_CHARS`: characters of PDF text the agent reads. The rest is cut. Default: `20000`. Minimum 1.
- `MEDIA_ROOT`: directory on local disk where uploaded images and audio are stored. Default: `./media`. Point it at a persistent volume.
- `MEDIA_MAX_BYTES`: largest accepted upload in bytes. Default: `16777216` (16 MiB).

## Queue and worker

- `DEBOUNCE_SECONDS`: seconds to wait for more messages from a customer before the agent answers. Each new message extends the wait. Default: `3`. Raise it for users who send many short messages.
- `WORKER_CONCURRENCY`: turns one worker process runs at once. Each running turn holds one database connection. Default: `4`. Minimum 1. Keep it within the database connection pool.
- `HUMANIZE_CHARS_PER_SEC`: typing speed used to pace reply parts. Default: `35.0`.
- `HUMANIZE_MIN_DELAY_SECONDS`: shortest pause before a reply part. Default: `0.8`.
- `HUMANIZE_MAX_DELAY_SECONDS`: longest pause before a reply part. Default: `4.0`.
- `HUMANIZE_MAX_PARTS`: most messages one reply is split into. Default: `4`. Set `1` to send one message.
- `SENTIMENT_THRESHOLD`: frustration score (0 to 1) at or above which an agent with sentiment escalation on hands the conversation to a human. A lower value escalates sooner. Default: `0.7`.
- `SENTIMENT_TIMEOUT_SECONDS`: time allowed for the sentiment check. Past it the check is skipped. Default: `5.0`.
- `CATEGORY_TIMEOUT_SECONDS`: time allowed for the category classifier. Past it the conversation keeps its category. Default: `5.0`. Must be above 0.
- `BOUNDARY_CHECK_TIMEOUT_SECONDS`: time allowed for the reply check against boundaries. Past it the check counts as failed and the reply is blocked. Default: `8.0`. Raise it if your model is slow.
- `MEMORY_CONTEXT_LIMIT`: how many long-term memories the agent gets each turn. Default: `20`. Range 1 to 100.

## Limits

- `RATE_LIMIT_TENANT_PER_MIN`: inbound messages per minute for one tenant. Past it the API answers 429. Default: `600`.
- `RATE_LIMIT_USER_PER_MIN`: inbound messages per minute for one customer on one channel. Past it the API answers 429. Default: `30`.
- `MAX_MESSAGE_CHARS`: longest accepted message text, in characters. Default: `4000`.
- `TOOL_MAX_RESPONSE_BYTES`: most bytes read from a tool's HTTP response. Default: `32768`.

## Tools and channels safety

- `TOOL_ALLOWED_HOSTS`: comma-separated host names a tool may call. Default: empty. When set, only these hosts are allowed, and the private-address checks are skipped for them. When empty, private, loopback and link-local addresses are blocked and public hosts are allowed. `docker-compose.yml` passes the value of `.env` to `api` and `worker`; it sets no host itself. The bundled `mock-tools` demo service is a private host: to try it set `TOOL_ALLOWED_HOSTS=mock-tools`, and remember the list is then exclusive, so add any real tool hosts to it (see [setup](setup.md) step 2).
- `TOOL_SSRF_DISABLED`: turn the tool address guard off. Default: `false`. Use it only in local development.
- `CHANNEL_ALLOWED_HOSTS`: comma-separated host names an Evolution channel `base_url` or an `external` channel `events_url` (checked on create and update, and again on every push) may use even if the address is private, for example `evolution`. Default: empty, so only public hosts are allowed. It does not depend on the `TOOL_` settings.
- `BOUNDARY_FLOOR_FILE`: path to a JSON file of boundaries applied to every agent of every tenant. Tenants can add to it but not loosen it. Default: empty, so there is no floor. A missing or broken file stops start-up. Shape: `{"scope": "optional text", "never": ["text"]}`.

## Events pushed to a project

Used by `external` channels. How the pushes work is in [project-channel](project-channel.md).

- `EVENT_PUSH_MAX_ATTEMPTS`: failed pushes of one event before parley stops pushing it (it stays in the feed). Default: `7`.
- `EVENT_PUSH_TIMEOUT_SECONDS`: longest wait for the project's answer to one push. Default: `5`.
- `EVENT_PUSH_INTERVAL_SECONDS`: pause between two passes of the push loop in the worker. Default: `1`.

## Demo

- `WEBCHAT_DEMO_API_KEY`: fixed key of the demo webchat tenant. Default: `demo-webchat-key`. Outside demo mode the API rejects this key with 401, so never use it in production.

## Docker compose variables

These are read by `docker-compose.yml`, not by the app: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_PORT`, `REDIS_PORT` and `API_PORT`. Compose reads `.env` for them. To change one, add a line such as `API_PORT=8002` to `.env` (`.env.example` has it commented out). `API_PORT` is the host port the API is published on, default `8001`; use it in your base URL, for example `BASE_URL=http://localhost:8002`.

## Check that it worked

Start the stack. If the API or the worker exits with a message naming `ADMIN_API_KEY` or `SECRETS_KEY`, fix that variable. Then `GET /health` returns 200.
