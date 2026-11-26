import uuid
from collections.abc import Sequence

from sqlalchemy import select

from app.models.mcp_server import McpServer
from app.models.mcp_tool import McpTool
from app.repositories.base import BaseRepository


class McpServersRepository(BaseRepository[McpServer]):
    model = McpServer

    async def get_by_name(self, tenant_id: uuid.UUID, name: str) -> McpServer | None:
        result = await self.session.execute(
            select(McpServer).where(McpServer.tenant_id == tenant_id, McpServer.name == name)
        )
        return result.scalar_one_or_none()

    async def tools(
        self, tenant_id: uuid.UUID, server_ids: Sequence[uuid.UUID]
    ) -> list[McpTool]:
        """The imported tools of these servers, by server then name."""
        if not server_ids:
            return []
        result = await self.session.execute(
            select(McpTool)
            .where(McpTool.tenant_id == tenant_id, McpTool.server_id.in_(list(server_ids)))
            .order_by(McpTool.server_id, McpTool.name)
        )
        return list(result.scalars().all())
