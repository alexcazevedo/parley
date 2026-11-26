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

An agent's `provider` picks the path. `openai_compatible` (the default) calls any OpenAI-compatible chat-completions endpoint, and uses the first four settings below, which belong to it alone. `openai`, `anthropic` and `google` call that vendor's own API and use the vendor's key setting. Every agent already stored keeps working on `openai_compatible` with nothing changed.

- `LLM_BASE_URL`: base URL of the provider. Default: `https://api.openai.com/v1`. Change it for another provider or a local server.
- `LLM_API_KEY`: key for the provider. Default: the placeholder `changeme-llm-key`. Set your real key.
- `LLM_MODEL`: default model name. Default: `gpt-4o-mini`. Change it to the model you use.
- `LLM_MAX_TOKENS_PARAM`: name of the token-limit field sent to the model. Default: `max_tokens`. Set `max_completion_tokens` for models that reject `max_tokens`. It applies to the whole deployment.
- `OPENAI_API_KEY`: key for an agent whose `provider` is `openai` (OpenAI's own API; `LLM_BASE_URL` does not apply). Default: empty, so that provider is unavailable.
- `ANTHROPIC_API_KEY`: key for an agent whose `provider` is `anthropic`. Default: empty.
- `GOOGLE_API_KEY`: key for an agent whose `provider` is `google` (the Gemini API). Default: empty.
- `HTTP_RETRY_ATTEMPTS`: tries for a model call, a speech-to-text call or a GET tool call that gets 429 or 5xx. The first try counts. Default: `3`. Minimum 1. Set `1` to turn retries off. On `openai`, `anthropic` and `google` the vendor's own client does the retrying, with the same count.
- `HTTP_RETRY_BASE_SECONDS`: first wait between tries. It doubles each time. A numeric `Retry-After` header replaces it. Default: `0.5`.
- `HTTP_RETRY_MAX_WAIT_SECONDS`: longest single wait. Default: `10.0`.

### A model per job

Besides the reply, a turn can make five more model calls ("jobs"). Each has a setting, the same for every tenant. Empty (the default) runs the job on the agent's own model with its text prompt, as before, whatever the agent's `provider`. `llm:<model>` runs it on that chat model at `LLM_BASE_URL` with `LLM_API_KEY`. `openai:<model>`, `anthropic:<model>` and `google:<model>` run it on that vendor's own API and need `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` or `GOOGLE_API_KEY`. `typesafe:<model>` runs a decision job on TypeSafe's decision model and needs `TYPESAFE_API_KEY`. On any of these a decision job gets a typed answer (a yes or no, a level, a pick); on a chat model an answer that does not fit is asked for once more, then counts as unusable: the reply is blocked with `check_unparseable`, the sentiment has no score, the department does not change. A writing job on a chat model writes text with its usual prompt. Any other value, a vendor form or `typesafe:` without its key, or `typesafe:` on a job that writes text stops the API and the worker at start, with a message naming the setting. Every job call is recorded in `model_job_calls` with the model that served it (as written in the setting, such as `openai:gpt-4o-mini`; a writing job on `llm:` records the bare model name), its token use, latency and outcome.

- `BOUNDARY_CHECK_MODEL`: the reply check against boundaries (a decision). Default: empty.
- `BOUNDARY_CHECK_ALLOW_THRESHOLD`: on a decision model, how likely "allowed" must be for a reply to go out, from 0 to 1. Higher is stricter: more replies are blocked. Default: `0.5`. It applies only to the boundary check on `typesafe:`; a chat model answers yes or no and ignores it. A value outside 0 to 1 stops the API and the worker at start. A blocked reply records the rule the model picked, or `no_rule_identified` when it picked none.
- `SENTIMENT_MODEL`: the frustration score (a decision). Default: empty.
- `CATEGORY_MODEL`: the category classifier (a decision). Default: empty.
- `SUMMARY_MODEL`: the rolling summary (writes text; `typesafe:` is refused). Default: empty.
- `HANDOFF_BRIEF_MODEL`: the brief for the operator at handoff (writes text; `typesafe:` is refused). Default: empty.
- `TYPESAFE_API_KEY`: key for TypeSafe's decision model. Required when any job is set to `typesafe:`. Default: empty.

What a job sends to TypeSafe, when it is set to `typesafe:<model>` (the key is sent to TypeSafe too). TypeSafe is a second vendor, besides your `LLM_BASE_URL` endpoint, and it receives conversation text:

- `BOUNDARY_CHECK_MODEL`: the customer's message and the agent's reply, and the agent's rules: its `boundary_scope` and `boundary_never` items, with the deployment floor's added.
- `SENTIMENT_MODEL`: the conversation text the sentiment check reads, both the customer's and the agent's side: the last 10 messages and the new ones (see [agent behaviour](agent-behaviour.md) section 4).
- `CATEGORY_MODEL`: the same kind of conversation text, and the name and description of each of the tenant's departments (categories).

Nothing is sent to TypeSafe unless `TYPESAFE_API_KEY` is set and a job is set to `typesafe:`. `llm:` values go only to your `LLM_BASE_URL` endpoint, and `openai:`, `anthropic:` and `google:` values only to that vendor, with the same text. A call the decision model cannot take is handed to the agent's model, so conversation text goes there as it does today; see [agent behaviour](agent-behaviour.md) section 8.

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

## MCP servers

A tenant's MCP servers are checked with the same address rules as tools (`TOOL_ALLOWED_HOSTS`, `TOOL_SSRF_DISABLED`), when registered and before every connection.

- `MCP_SYNC_INTERVAL_SECONDS`: how old a server's last discovery may get before the worker discovers its tools again. A server whose discovery failed is tried again after the same time. Default: `300`.

## Events pushed to a project

Used by `external` channels. How the pushes work is in [project-channel](project-channel.md).

- `EVENT_PUSH_MAX_ATTEMPTS`: failed pushes of one event before parley stops pushing it (it stays in the feed). Default: `7`.
- `EVENT_PUSH_TIMEOUT_SECONDS`: longest wait for the project's answer to one push. Default: `5`.
- `EVENT_PUSH_INTERVAL_SECONDS`: pause between two passes of the push loop in the worker. Default: `1`.

## Knowledge base

Counted per tenant, the same for every tenant. A request past a limit is refused with 422 naming the setting.

- `KNOWLEDGE_MAX_ITEMS`: most knowledge items (entries, pairs and documents) of one tenant. Default: `500`.
- `KNOWLEDGE_MAX_ITEM_CHARS`: longest text of one entry or answer, in characters. Default: `20000`.
- `KNOWLEDGE_MAX_TOTAL_CHARS`: most characters of knowledge text stored by one tenant. Default: `2000000`.
- `KNOWLEDGE_INGEST_INTERVAL_SECONDS`: pause between two passes of the worker loop that splits new and edited items into passages. Default: `2`.

Semantic search is optional. With an embeddings endpoint set, the worker stores a vector for every passage and a search also ranks passages by meaning, merged with the keyword ranking (`mode` `hybrid`). It needs the Postgres `vector` extension: the compose image `pgvector/pgvector:pg16` has it; on plain `postgres:16` search stays keyword only. After changing the model, or to embed passages written before the endpoint was set, run `uv run python -m app.knowledge.reindex` (`--all` re-embeds every passage).

- `EMBEDDINGS_BASE_URL`: base URL of an OpenAI-compatible embeddings endpoint (parley calls `{base}/embeddings`). Default: empty, which turns semantic search off.
- `EMBEDDINGS_API_KEY`: bearer key for that endpoint. Default: empty.
- `EMBEDDINGS_MODEL`: embeddings model. Vectors of another model are ignored until re-indexed. Default: `text-embedding-3-small`.

## Demo

- `WEBCHAT_DEMO_API_KEY`: fixed key of the demo webchat tenant. Default: `demo-webchat-key`. Outside demo mode the API rejects this key with 401, so never use it in production.

## Docker compose variables

These are read by `docker-compose.yml`, not by the app: `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_PORT`, `REDIS_PORT` and `API_PORT`. Compose reads `.env` for them. To change one, add a line such as `API_PORT=8002` to `.env` (`.env.example` has it commented out). `API_PORT` is the host port the API is published on, default `8001`; use it in your base URL, for example `BASE_URL=http://localhost:8002`.

## Check that it worked

Start the stack. If the API or the worker exits with a message naming `ADMIN_API_KEY` or `SECRETS_KEY`, fix that variable. Then `GET /health` returns 200.
