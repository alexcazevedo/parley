"""McpServer model: a tenant's MCP server (streamable HTTP) whose tools parley imports."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped, pg_enum


class McpServerStatus(enum.StrEnum):
    # Saved, not discovered yet.
    PENDING = "pending"
    # The last discovery worked.
    OK = "ok"
    # The last discovery failed (`error` says why); the last known tools stay.
    ERROR = "error"


class McpServer(Base, TenantScoped):
    __tablename__ = "mcp_servers"
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_mcp_servers_tenant_id_name"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(24), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    # `{"type": "bearer"|"header", "token", "header_name"?}`, Fernet-encrypted.
    # Never returned by the API (app/api/mcp_servers.py).
    auth_config_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Tool names to import; null imports every tool.
    allowed_tools: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    timeout_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    status: Mapped[McpServerStatus] = mapped_column(
        pg_enum(McpServerStatus, "mcp_server_status"),
        nullable=False,
        default=McpServerStatus.PENDING,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Tool names not imported because their exposed name is invalid.
    skipped: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # Time of the last discovery attempt, successful or not (app/mcp/sync.py).
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
