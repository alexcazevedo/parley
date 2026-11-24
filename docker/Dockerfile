FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.6 /uv /uvx /bin/

# The image is built once with `uv sync --frozen --no-dev`; don't let
# `uv run` re-resolve/sync (and pull dev-only deps like ruff/pytest) at
# container start. No cache: the app user needs nothing writable for `uv run`.
ENV UV_NO_SYNC=1 \
    UV_NO_CACHE=1

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /home/app \
        --shell /usr/sbin/nologin app \
    && mkdir -p /home/app /srv/media \
    && chown -R app:app /home/app /srv

WORKDIR /srv

COPY --chown=app:app pyproject.toml uv.lock /srv/
RUN chown app:app /srv && su -s /bin/sh app -c "uv sync --frozen --no-dev"

COPY --chown=app:app app /srv/app
COPY --chown=app:app migrations /srv/migrations
COPY --chown=app:app alembic.ini /srv/alembic.ini
COPY --chown=app:app llms.txt /srv/llms.txt
COPY --chown=app:app docs /srv/docs

EXPOSE 8000

USER app

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
