import uuid

from sqlalchemy import select

from app.models.category import Category
from app.repositories.base import BaseRepository


class CategoriesRepository(BaseRepository[Category]):
    model = Category

    async def list(self, tenant_id: uuid.UUID, **filters) -> list[Category]:
        stmt = select(Category).where(Category.tenant_id == tenant_id)
        for key, value in filters.items():
            stmt = stmt.where(getattr(Category, key) == value)
        result = await self.session.execute(stmt.order_by(Category.name, Category.id))
        return list(result.scalars().all())
