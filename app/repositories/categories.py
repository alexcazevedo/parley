import builtins
import uuid
from typing import Any, cast

from sqlalchemy import select, tuple_

from app.models.category import Category
from app.repositories.base import BaseRepository, bad_cursor, parse_uuid


class CategoriesRepository(BaseRepository[Category]):
    model = Category

    async def list(self, tenant_id: uuid.UUID, **filters: Any) -> list[Category]:
        stmt = select(Category).where(Category.tenant_id == tenant_id)
        for key, value in filters.items():
            stmt = stmt.where(getattr(Category, key) == value)
        result = await self.session.execute(stmt.order_by(Category.name, Category.id))
        return list(result.scalars().all())

    async def list_page(
        self,
        tenant_id: uuid.UUID,
        *,
        limit: int,
        after: tuple[str, uuid.UUID] | None = None,
        **filters: Any,
    ) -> "builtins.list[Category]":
        """Keyset page ordered by `(name, id)`; `after` is the last `(name, id)` seen."""
        stmt = select(Category).where(Category.tenant_id == tenant_id)
        for key, value in filters.items():
            stmt = stmt.where(getattr(Category, key) == value)
        if after is not None:
            stmt = stmt.where(
                tuple_(Category.name, Category.id)
                > tuple_(cast(Any, after[0]), cast(Any, after[1]))
            )
        result = await self.session.execute(
            stmt.order_by(Category.name, Category.id).limit(limit)
        )
        return list(result.scalars().all())


def parse_name_after(values: dict[str, Any] | None) -> tuple[str, uuid.UUID] | None:
    if values is None:
        return None
    if not isinstance(values.get("name"), str) or "id" not in values:
        raise bad_cursor()
    return values["name"], parse_uuid(values["id"])
