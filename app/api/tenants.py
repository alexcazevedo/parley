"""Tenant registration (admin-only) and self-lookup (per-tenant key)."""

import uuid

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.models.tenant import Tenant
from app.security.auth import generate_api_key, hash_api_key, require_admin

router = APIRouter(tags=["tenants"])


class TenantCreate(BaseModel):
    name: str


class TenantCreated(BaseModel):
    id: uuid.UUID
    name: str
    api_key: str


class TenantOut(BaseModel):
    id: uuid.UUID
    name: str

    model_config = {"from_attributes": True}


@router.post(
    "/tenants",
    response_model=TenantCreated,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_admin)],
)
async def create_tenant(payload: TenantCreate, db: AsyncSession = Depends(get_db)) -> TenantCreated:
    plaintext_key = generate_api_key()
    tenant = Tenant(name=payload.name, api_key_hash=hash_api_key(plaintext_key))
    db.add(tenant)
    await db.commit()
    await db.refresh(tenant)
    return TenantCreated(id=tenant.id, name=tenant.name, api_key=plaintext_key)


@router.get("/tenants/me", response_model=TenantOut)
async def read_current_tenant(tenant: Tenant = Depends(get_current_tenant)) -> Tenant:
    return tenant
