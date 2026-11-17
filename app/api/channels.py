"""CRUD REST for channels. `secret` is write-only: encrypted on the way in via
`app.security.secrets`, never returned in any response.
"""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.models.channel import ChannelType
from app.models.tenant import Tenant
from app.repositories.agents import AgentsRepository
from app.repositories.channels import ChannelsRepository
from app.security import secrets as secrets_crypto
from app.tools.ssrf import SSRFError, validate_channel_base_url

router = APIRouter(prefix="/channels", tags=["channels"])


class ChannelCreate(BaseModel):
    type: ChannelType
    name: str
    agent_id: uuid.UUID
    config: dict[str, Any] = Field(default_factory=dict)
    secret: str | None = None


class ChannelUpdate(BaseModel):
    name: str | None = None
    agent_id: uuid.UUID | None = None
    config: dict[str, Any] | None = None
    secret: str | None = None


class ChannelOut(BaseModel):
    id: uuid.UUID
    type: ChannelType
    name: str
    agent_id: uuid.UUID
    config: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


def _repo(db: AsyncSession) -> ChannelsRepository:
    return ChannelsRepository(db)


async def _require_agent(db: AsyncSession, tenant: Tenant, agent_id: uuid.UUID) -> None:
    # Same answer for missing and foreign ids: no existence oracle.
    if await AgentsRepository(db).get(tenant.id, agent_id) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="agent not found"
        )


def _check_endpoint(channel_type: ChannelType, config: dict[str, Any] | None) -> None:
    """Refuse a non-public Evolution `base_url` (PRODUCT.md F1) before any write.

    Static check only (no DNS); the adapter re-checks with DNS on every call.
    """
    if channel_type != ChannelType.WHATSAPP_EVOLUTION or not config or "base_url" not in config:
        return
    try:
        validate_channel_base_url(str(config["base_url"]))
    except SSRFError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"invalid base_url: {exc}",
        ) from exc


@router.post("", response_model=ChannelOut, status_code=status.HTTP_201_CREATED)
async def create_channel(
    payload: ChannelCreate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ChannelOut:
    _check_endpoint(payload.type, payload.config)
    await _require_agent(db, tenant, payload.agent_id)
    data = payload.model_dump()
    secret = data.pop("secret")
    if secret is not None:
        data["secret_encrypted"] = secrets_crypto.encrypt(secret)
    channel = await _repo(db).create(tenant.id, **data)
    await db.commit()
    await db.refresh(channel)
    return ChannelOut.model_validate(channel)


@router.get("", response_model=list[ChannelOut])
async def list_channels(
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[ChannelOut]:
    channels = await _repo(db).list(tenant.id)
    return [ChannelOut.model_validate(c) for c in channels]


@router.get("/{channel_id}", response_model=ChannelOut)
async def get_channel(
    channel_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ChannelOut:
    channel = await _repo(db).get(tenant.id, channel_id)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    return ChannelOut.model_validate(channel)


@router.patch("/{channel_id}", response_model=ChannelOut)
async def update_channel(
    channel_id: uuid.UUID,
    payload: ChannelUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ChannelOut:
    existing = await _repo(db).get(tenant.id, channel_id)
    if existing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    changes = payload.model_dump(exclude_unset=True)
    if "config" in changes:
        _check_endpoint(existing.type, changes["config"])
    if "agent_id" in changes:
        if changes["agent_id"] is None:
            # A null agent_id is ignored: the column is non-null, so the
            # channel keeps its current agent.
            changes.pop("agent_id")
        else:
            await _require_agent(db, tenant, changes["agent_id"])
    if "secret" in changes:
        secret = changes.pop("secret")
        changes["secret_encrypted"] = secrets_crypto.encrypt(secret) if secret is not None else None
    channel = await _repo(db).update(tenant.id, channel_id, **changes)
    if channel is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    await db.commit()
    await db.refresh(channel)
    return ChannelOut.model_validate(channel)


@router.delete("/{channel_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_channel(
    channel_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> None:
    deleted = await _repo(db).delete(tenant.id, channel_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="channel not found")
    await db.commit()
