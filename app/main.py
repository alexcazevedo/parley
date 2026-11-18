"""FastAPI application entrypoint. Register new routers here."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agents.boundaries import load_floor
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
from app.config import Settings, assert_secure_settings, get_settings
from app.http_client import close_shared_client
from app.observability.logging import configure_logging, request_id_middleware

configure_logging()


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await close_shared_client()


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the API. Refuses insecure settings outside demo mode, and mounts
    the demo surface (`/chat`, `/chat/bootstrap`, `/dev/*`) only in demo mode.
    """
    settings = settings if settings is not None else get_settings()
    assert_secure_settings(settings)
    load_floor(settings.boundary_floor_file)

    app = FastAPI(title="Conversational Agent Harness", lifespan=_lifespan)

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
    if settings.demo_mode:
        app.include_router(chat.router)
    app.include_router(operator.router)
    if settings.demo_mode:
        app.include_router(dev.router)
    app.include_router(webhooks.router)
    return app


app = create_app()
