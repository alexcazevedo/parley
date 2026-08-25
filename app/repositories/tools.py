import uuid

from sqlalchemy import select

from app.models.tool import Tool
from app.repositories.base import BaseRepository


class ToolsRepository(BaseRepository[Tool]):
    model = Tool

    async def get_by_name(self, tenant_id: uuid.UUID, name: str) -> Tool | None:
        result = await self.session.execute(
            select(Tool).where(Tool.tenant_id == tenant_id, Tool.name == name)
        )
        return result.scalar_one_or_none()
