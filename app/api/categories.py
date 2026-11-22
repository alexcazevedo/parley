"""CRUD REST for the tenant's categories (the subjects conversations are filed under)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, StringConstraints
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_tenant, get_db
from app.models.tenant import Tenant
from app.repositories.categories import CategoriesRepository

router = APIRouter(prefix="/categories", tags=["categories"])

CategoryName = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]


class CategoryCreate(BaseModel):
    name: CategoryName
    description: str | None = None


class CategoryUpdate(BaseModel):
    name: CategoryName | None = None
    description: str | None = None


class CategoryOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


def _repo(db: AsyncSession) -> CategoriesRepository:
    return CategoriesRepository(db)


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="category not found")


def _duplicate() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail="category name already exists"
    )


@router.post("", response_model=CategoryOut, status_code=status.HTTP_201_CREATED)
async def create_category(
    payload: CategoryCreate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> CategoryOut:
    try:
        category = await _repo(db).create(tenant.id, **payload.model_dump())
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _duplicate() from exc
    await db.refresh(category)
    return CategoryOut.model_validate(category)


@router.get("", response_model=list[CategoryOut])
async def list_categories(
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> list[CategoryOut]:
    return [CategoryOut.model_validate(c) for c in await _repo(db).list(tenant.id)]


@router.get("/{category_id}", response_model=CategoryOut)
async def get_category(
    category_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> CategoryOut:
    category = await _repo(db).get(tenant.id, category_id)
    if category is None:
        raise _not_found()
    return CategoryOut.model_validate(category)


@router.patch("/{category_id}", response_model=CategoryOut)
async def update_category(
    category_id: uuid.UUID,
    payload: CategoryUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> CategoryOut:
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("name") is None:
        changes.pop("name", None)
    try:
        category = await _repo(db).update(tenant.id, category_id, **changes)
        if category is None:
            raise _not_found()
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise _duplicate() from exc
    await db.refresh(category)
    return CategoryOut.model_validate(category)


@router.delete("/{category_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_category(
    category_id: uuid.UUID,
    tenant: Tenant = Depends(get_current_tenant),
    db: AsyncSession = Depends(get_db),
) -> None:
    if not await _repo(db).delete(tenant.id, category_id):
        raise _not_found()
    await db.commit()
