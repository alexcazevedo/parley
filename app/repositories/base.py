"""Generic tenant-scoped repository.

Every method takes `tenant_id` explicitly and filters on it — no query in this
codebase should ever reach the database without a tenant scope. Shaped this
way so PostgreSQL row-level security can be layered on later without changing
callers.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.base import Base


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
