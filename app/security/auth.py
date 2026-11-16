"""Auth dependencies: admin key for /tenants, per-tenant API key for everything else."""

import hashlib
import hmac
import secrets

import structlog
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.models.tenant import Tenant

logger = structlog.get_logger(__name__)


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def generate_api_key() -> str:
    return secrets.token_urlsafe(32)


async def require_admin(x_admin_key: str | None = Header(default=None)) -> None:
    settings = get_settings()
    if not x_admin_key or not secrets.compare_digest(x_admin_key, settings.admin_api_key):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid admin key")


async def get_current_tenant(
    x_api_key: str | None = Header(default=None),
    db: AsyncSession = Depends(get_db),
) -> Tenant:
    if not x_api_key:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing API key")
    settings = get_settings()
    # The demo tenant's key is public (shipped in the repo). Outside demo mode
    # a leftover demo tenant row must not be usable: answer as for an unknown
    # key, before touching the DB.
    if not settings.demo_mode and hmac.compare_digest(
        x_api_key.encode("utf-8"), settings.webchat_demo_api_key.encode("utf-8")
    ):
        logger.warning("auth.demo_key_rejected")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")
    key_hash = hash_api_key(x_api_key)
    result = await db.execute(select(Tenant).where(Tenant.api_key_hash == key_hash))
    tenant = result.scalar_one_or_none()
    if tenant is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")
    return tenant
