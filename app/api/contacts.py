"""CRUD REST for contacts."""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.api.pagination import DEFAULT_LIMIT, MAX_LIMIT, decode_cursor, set_next_cursor
from app.memory.service import MemoryService
from app.models.memory import Memory, MemoryType
from app.models.tenant import Tenant
from app.repositories.base import parse_after
from app.repositories.channels import ChannelsRepository
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
    if await ChannelsRepository(db).get(tenant.id, payload.channel_id) is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="channel not found"
        )
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
    response: Response,
    limit: int = Query(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    cursor: str | None = Query(default=None),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[ContactOut]:
    after = parse_after(decode_cursor(cursor))
    rows = await _repo(db).list_page(tenant.id, limit=limit + 1, after=after)
    more = len(rows) > limit
    rows = rows[:limit]
    if more:
        set_next_cursor(
            response, {"created_at": rows[-1].created_at.isoformat(), "id": str(rows[-1].id)}
        )
    return [ContactOut.model_validate(c) for c in rows]


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


# --- Long-term memory CRUD --------------------------------------


class MemoryCreate(BaseModel):
    type: MemoryType
    content: str
    importance: int = Field(default=3, ge=1, le=5)
    expires_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryOut(BaseModel):
    id: uuid.UUID
    contact_id: uuid.UUID
    type: MemoryType
    content: str
    importance: int
    metadata: dict[str, Any]
    last_accessed_at: datetime | None
    expires_at: datetime | None
    created_at: datetime

    @classmethod
    def from_model(cls, memory: Memory) -> "MemoryOut":
        # `Memory.metadata` is exposed on the model as `.meta` (`metadata` is
        # reserved by SQLAlchemy's declarative Base) — built explicitly rather
        # than via `model_validate(memory, from_attributes=True)` so the field
        # rename doesn't depend on alias-vs-attribute lookup nuances.
        return cls(
            id=memory.id,
            contact_id=memory.contact_id,
            type=memory.type,
            content=memory.content,
            importance=memory.importance,
            metadata=memory.meta,
            last_accessed_at=memory.last_accessed_at,
            expires_at=memory.expires_at,
            created_at=memory.created_at,
        )


async def _get_contact_or_404(db: AsyncSession, tenant: Tenant, contact_id: uuid.UUID):
    contact = await _repo(db).get(tenant.id, contact_id)
    if contact is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="contact not found")
    return contact


@router.get("/{contact_id}/memories", response_model=list[MemoryOut])
async def list_contact_memories(
    contact_id: uuid.UUID,
    query: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[MemoryOut]:
    await _get_contact_or_404(db, tenant, contact_id)
    service = MemoryService(db)
    if query:
        memories = await service.search(tenant.id, contact_id, query, limit=limit)
    else:
        memories = await service.get_contact_memories(tenant.id, contact_id, limit=limit)
    return [MemoryOut.from_model(m) for m in memories]


@router.post(
    "/{contact_id}/memories", response_model=MemoryOut, status_code=status.HTTP_201_CREATED
)
async def create_contact_memory(
    contact_id: uuid.UUID,
    payload: MemoryCreate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> MemoryOut:
    await _get_contact_or_404(db, tenant, contact_id)
    memory = await MemoryService(db).store(
        tenant.id,
        contact_id,
        type=payload.type,
        content=payload.content,
        importance=payload.importance,
        expires_at=payload.expires_at,
        metadata=payload.metadata,
    )
    await db.commit()
    await db.refresh(memory)
    return MemoryOut.from_model(memory)


@router.delete("/{contact_id}/memories/{memory_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_contact_memory(
    contact_id: uuid.UUID,
    memory_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> None:
    await _get_contact_or_404(db, tenant, contact_id)
    memory = await db.get(Memory, memory_id)
    if memory is None or memory.tenant_id != tenant.id or memory.contact_id != contact_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="memory not found")
    await MemoryService(db).forget(tenant.id, memory_id)
    await db.commit()
