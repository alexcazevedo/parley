"""FastAPI application entrypoint. Register new routers here."""

from fastapi import FastAPI

from app.api import (
    agents,
    channels,
    chat,
    contacts,
    conversations,
    dev,
    health,
    media,
    operator,
    tenants,
    tools,
    webchat,
    webhooks,
)
from app.api.errors import add_exception_handlers
from app.observability.logging import configure_logging, request_id_middleware

configure_logging()

app = FastAPI(title="Conversational Agent Harness")

app.middleware("http")(request_id_middleware)
add_exception_handlers(app)

app.include_router(health.router)
app.include_router(tenants.router)
app.include_router(agents.router)
app.include_router(channels.router)
app.include_router(tools.router)
app.include_router(contacts.router)
app.include_router(conversations.router)
app.include_router(webchat.router)
app.include_router(media.router)
app.include_router(chat.router)
app.include_router(operator.router)
app.include_router(dev.router)
app.include_router(webhooks.router)
