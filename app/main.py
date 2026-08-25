"""FastAPI application entrypoint. Register new routers here."""

from fastapi import FastAPI

from app.api import health, tenants
from app.observability.logging import configure_logging, request_id_middleware

configure_logging()

app = FastAPI(title="Conversational Agent Harness")

app.middleware("http")(request_id_middleware)

app.include_router(health.router)
app.include_router(tenants.router)
