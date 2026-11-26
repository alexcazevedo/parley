"""ManifestItem: what the manifest manages (agent, department, tool, channel, knowledge).

`key` is the author's key for agents and channels and the name for
departments and tools. `item_id` points at the managed row; it is not a
foreign key because the target table depends on `kind`.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import CheckConstraint, DateTime, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped

MANIFEST_KINDS = (
    "agent",
    "department",
    "tool",
    "channel",
    "knowledge_entry",
    "knowledge_qa",
)


class ManifestItem(Base, TenantScoped):
    __tablename__ = "manifest_items"
    __table_args__ = (
        UniqueConstraint("tenant_id", "kind", "key", name="uq_manifest_items_tenant_kind_key"),
        UniqueConstraint(
            "tenant_id", "kind", "item_id", name="uq_manifest_items_tenant_kind_item"
        ),
        CheckConstraint(
            "kind IN ('agent', 'department', 'tool', 'channel', "
            "'knowledge_entry', 'knowledge_qa')",
            name="ck_manifest_items_kind",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    item_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # The secret name each credential was applied with, by field: `{"token":
    # "orders_token"}` for a tool, `{"secret": "evolution_key"}` for a channel.
    secret_names: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
