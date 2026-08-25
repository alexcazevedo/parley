FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"
# The image is built once with `uv sync --frozen --no-dev`; don't let
# `uv run` re-resolve/sync (and pull dev-only deps like ruff/pytest) at
# container start.
ENV UV_NO_SYNC=1

WORKDIR /srv

COPY pyproject.toml uv.lock /srv/
RUN uv sync --frozen --no-dev

COPY app /srv/app
COPY migrations /srv/migrations
COPY alembic.ini /srv/alembic.ini

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
