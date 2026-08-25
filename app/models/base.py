"""Declarative base and shared mixins for all ORM models."""

import uuid

from sqlalchemy import Enum as SAEnum
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


def pg_enum(enum_cls: type, name: str) -> SAEnum:
    """Native Postgres enum that persists member *values* (matching the
    Alembic migrations), not Python member names."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda e: [m.value for m in e],
    )
