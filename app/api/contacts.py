"""CRUD REST for contacts."""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.models.tenant import Tenant
from app.repositories.contacts import ContactsRepository

router = APIRouter(prefix="/contacts", tags=["contacts"])


class ContactCreate(BaseModel):
    channel_id: uuid.UUID
    external_user_id: str
    display_name: str | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class ContactUpdate(BaseModel):
    display_name: str | None = None
    attributes: dict[str, Any] | None = None


class ContactOut(BaseModel):
    id: uuid.UUID
    channel_id: uuid.UUID
    external_user_id: str
    display_name: str | None
    attributes: dict[str, Any]
    created_at: datetime

    model_config = {"from_attributes": True}


def _repo(db: AsyncSession) -> ContactsRepository:
    return ContactsRepository(db)


@router.post("", response_model=ContactOut, status_code=status.HTTP_201_CREATED)
async def create_contact(
    payload: ContactCreate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ContactOut:
    try:
        contact = await _repo(db).create(tenant.id, **payload.model_dump())
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="contact already exists for this channel",
        ) from exc
    await db.refresh(contact)
    return ContactOut.model_validate(contact)


@router.get("", response_model=list[ContactOut])
async def list_contacts(
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[ContactOut]:
    contacts = await _repo(db).list(tenant.id)
    return [ContactOut.model_validate(c) for c in contacts]


@router.get("/{contact_id}", response_model=ContactOut)
async def get_contact(
    contact_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ContactOut:
    contact = await _repo(db).get(tenant.id, contact_id)
    if contact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="contact not found")
    return ContactOut.model_validate(contact)


@router.patch("/{contact_id}", response_model=ContactOut)
async def update_contact(
    contact_id: uuid.UUID,
    payload: ContactUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> ContactOut:
    changes = payload.model_dump(exclude_unset=True)
    contact = await _repo(db).update(tenant.id, contact_id, **changes)
    if contact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="contact not found")
    await db.commit()
    await db.refresh(contact)
    return ContactOut.model_validate(contact)


@router.delete("/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contact(
    contact_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> None:
    deleted = await _repo(db).delete(tenant.id, contact_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="contact not found")
    await db.commit()
