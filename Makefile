SHELL := /bin/bash

.PHONY: help env db lint typecheck test check

help: ## List the targets
	@grep -E '^[a-zA-Z_-]+:.*## ' Makefile | awk 'BEGIN {FS = ":.*## "} {printf "  %-10s %s\n", $$1, $$2}'

env: ## Copy .env.example to .env when .env does not exist
	@[ -f .env ] || cp .env.example .env

db: env ## Start the Postgres container unless one already accepts connections
	@if (echo > /dev/tcp/localhost/$${POSTGRES_PORT:-5434}) 2>/dev/null; then \
		echo "database already running"; \
	else \
		docker compose up -d --wait postgres; \
	fi

lint: ## Run the linter (ruff)
	uv run ruff check .

typecheck: ## Run the type checker (mypy)
	uv run mypy

test: db ## Start the database when needed and run the suite (ARGS=... for one file)
	uv run pytest -q $(ARGS)

check: lint typecheck test ## Run lint, type check, then the test suite
