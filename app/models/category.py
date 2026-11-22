"""Category model: a subject the tenant defined; the classifier picks from this list."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped


class Category(Base, TenantScoped):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_categories_tenant_name"),)

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    # The text the model reads to decide whether a conversation belongs here.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
