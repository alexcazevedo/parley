"""Generic tenant-scoped repository.

Every method takes `tenant_id` explicitly and filters on it — no query in this
codebase should ever reach the database without a tenant scope. Shaped this
way so PostgreSQL row-level security can be layered on later without changing
callers.
"""

import uuid
from datetime import datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base


def bad_cursor() -> HTTPException:
    return HTTPException(status_code=422, detail="invalid cursor")


def parse_dt(value: Any) -> datetime:
    """An ISO timestamp from a cursor; must carry a timezone."""
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise bad_cursor() from None
    if parsed.tzinfo is None:
        raise bad_cursor()
    return parsed


def parse_uuid(value: Any) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        raise bad_cursor() from None


def parse_after(values: dict | None) -> tuple[datetime, uuid.UUID] | None:
    """`{"created_at", "id"}` cursor values -> the `after` tuple for `list_page`."""
    if values is None:
        return None
    if "created_at" not in values or "id" not in values:
        raise bad_cursor()
    return parse_dt(values["created_at"]), parse_uuid(values["id"])


class BaseRepository[ModelT: Base]:
    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, tenant_id: uuid.UUID, id: uuid.UUID) -> ModelT | None:
        result = await self.session.execute(
            select(self.model).where(
                self.model.tenant_id == tenant_id, self.model.id == id
            )
        )
        return result.scalar_one_or_none()

    async def list(self, tenant_id: uuid.UUID, **filters: Any) -> list[ModelT]:
        stmt = select(self.model).where(self.model.tenant_id == tenant_id)
        for key, value in filters.items():
            stmt = stmt.where(getattr(self.model, key) == value)
        stmt = stmt.order_by(self.model.created_at)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_page(
        self,
        tenant_id: uuid.UUID,
        *,
        limit: int,
        after: tuple[datetime, uuid.UUID] | None = None,
        **filters: Any,
    ) -> "list[ModelT]":
        """Keyset page ordered by `(created_at, id)`: up to `limit` rows after
        `after` (the `(created_at, id)` of the last row seen). Callers pass
        `page size + 1` to learn whether another page exists.
        """
        stmt = select(self.model).where(self.model.tenant_id == tenant_id)
        for key, value in filters.items():
            stmt = stmt.where(getattr(self.model, key) == value)
        if after is not None:
            stmt = stmt.where(
                tuple_(self.model.created_at, self.model.id) > tuple_(after[0], after[1])
            )
        stmt = stmt.order_by(self.model.created_at, self.model.id).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def create(self, tenant_id: uuid.UUID, **kwargs: Any) -> ModelT:
        obj = self.model(tenant_id=tenant_id, **kwargs)
        self.session.add(obj)
        await self.session.flush()
        return obj

    async def update(self, tenant_id: uuid.UUID, id: uuid.UUID, **kwargs: Any) -> ModelT | None:
        obj = await self.get(tenant_id, id)
        if obj is None:
            return None
        for key, value in kwargs.items():
            setattr(obj, key, value)
        await self.session.flush()
        return obj

    async def delete(self, tenant_id: uuid.UUID, id: uuid.UUID) -> bool:
        obj = await self.get(tenant_id, id)
        if obj is None:
            return False
        await self.session.delete(obj)
        await self.session.flush()
        return True
