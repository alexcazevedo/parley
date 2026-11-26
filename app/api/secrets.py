"""Tenant secrets: set, replace, list by name and delete; a value is never read back."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.models.manifest_item import ManifestItem
from app.models.tenant import Tenant
from app.repositories.secrets import SecretsRepository

router = APIRouter(prefix="/secrets", tags=["secrets"])

SECRET_NAME_PATTERN = r"^[a-z0-9_]{1,64}$"
SecretName = Annotated[str, Path(pattern=SECRET_NAME_PATTERN)]


class SecretSet(BaseModel):
    value: str = Field(min_length=1, max_length=8192)


class SecretOut(BaseModel):
    name: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


@router.put("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def set_secret(
    name: SecretName,
    payload: SecretSet,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> None:
    await SecretsRepository(db).set(tenant.id, name, payload.value)
    await db.commit()


@router.get("", response_model=list[SecretOut])
async def list_secrets(
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[SecretOut]:
    rows = await SecretsRepository(db).names(tenant.id)
    return [SecretOut.model_validate(r) for r in rows]


async def _users(db: AsyncSession, tenant_id: uuid.UUID, name: str) -> list[str]:
    """`tool 'key'` / `channel 'key'` of every managed item that references `name`."""
    result = await db.execute(
        select(ManifestItem.kind, ManifestItem.key, ManifestItem.secret_names)
        .where(ManifestItem.tenant_id == tenant_id, ManifestItem.kind.in_(("tool", "channel")))
        .order_by(ManifestItem.kind.desc(), ManifestItem.key)
    )
    return [
        f"{kind} '{key}'"
        for kind, key, names in result.all()
        if name in (names or {}).values()
    ]


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_secret(
    name: SecretName,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> None:
    users = await _users(db, tenant.id, name)
    if users:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail=f"secret is used by {', '.join(users)}"
        )
    if not await SecretsRepository(db).delete(tenant.id, name):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="secret not found")
    await db.commit()
