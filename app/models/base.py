"""Declarative base and shared mixins for all ORM models."""

import uuid

from sqlalchemy import ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TenantScoped:
    """Mixin for every table that belongs to a tenant.

    Shaped so PostgreSQL row-level security can be layered on later:
    every tenant-owned row carries an indexed `tenant_id` FK.
    """

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
