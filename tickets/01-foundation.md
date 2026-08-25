---
id: 01
status: done
model: sonnet
depends_on: []
max_attempts: 3
files:
  - pyproject.toml
  - .env.example
  - .gitignore
  - docker-compose.yml
  - docker/Dockerfile
  - alembic.ini
  - migrations/env.py
  - migrations/script.py.mako
  - migrations/versions/0001_tenants.py
  - app/__init__.py
  - app/config.py
  - app/db.py
  - app/main.py
  - app/models/__init__.py
  - app/models/base.py
  - app/models/tenant.py
  - app/observability/__init__.py
  - app/observability/logging.py
  - app/security/__init__.py
  - app/security/auth.py
  - app/api/__init__.py
  - app/api/deps.py
  - app/api/health.py
  - app/api/tenants.py
  - app/workers/__init__.py
  - app/workers/main.py
  - tests/__init__.py
  - tests/conftest.py
  - tests/test_health.py
  - tests/test_tenants.py
---
## Goal
Bootable FastAPI + Postgres + Redis skeleton with tenants, admin key, per-tenant API key auth, `/health`, Alembic, Docker Compose, structured logging and a pytest harness.

## Context
Greenfield. Python 3.12, `uv` + `pyproject.toml` (deps: fastapi, uvicorn[standard], sqlalchemy[asyncio]>=2, asyncpg, alembic, pydantic-settings, redis>=5, structlog, httpx, pytest, pytest-asyncio, ruff). Modular monolith under `app/`.

`app/config.py`: pydantic `Settings` from env — `DATABASE_URL`, `REDIS_URL`, `ADMIN_API_KEY`, `SECRETS_KEY` (Fernet), `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`, `DEBOUNCE_SECONDS=3`, `LOG_LEVEL`.

`models/base.py`: `Base`, `TenantScoped` mixin (`tenant_id: UUID FK, indexed`). `Tenant(id uuid, name, api_key_hash sha256, created_at)`. `POST /tenants` (admin key via `X-Admin-Key`) returns plaintext key once. `security/auth.py`: `get_current_tenant` dep reads `X-API-Key`, hashes, looks up → 401 otherwise.

Compose: `api`, `worker` (runs `app.workers.main`, loops logging heartbeat for now), `postgres:16` on 5434, `redis:7` on 6380. Tests hit `harness_test` DB (create/drop tables per session), `fakeredis` for Redis. structlog JSON, `request_id` middleware.

## Checks (run in order — cheapest first)
1. `uv run ruff check .`
2. `docker compose up -d postgres redis && uv run alembic upgrade head`
3. `uv run pytest -q`
4. `docker compose up -d api && sleep 3 && curl -sf localhost:8001/health | grep ok`

## Done when
- `POST /tenants` with admin key creates tenant, returns key; same key on `GET /tenants/me` returns tenant; wrong key → 401.
- `docker compose up` starts all four services; `/docs` renders.

## Not included
- Any entity beyond Tenant; queue; channels; LLM.
