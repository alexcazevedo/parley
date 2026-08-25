"""Channel model: one inbound/outbound surface (webchat, WhatsApp) bound to an agent."""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TenantScoped, pg_enum


class ChannelType(enum.StrEnum):
    WEBCHAT = "webchat"
    WHATSAPP_EVOLUTION = "whatsapp_evolution"


class Channel(Base, TenantScoped):
    __tablename__ = "channels"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    type: Mapped[ChannelType] = mapped_column(
        pg_enum(ChannelType, "channel_type"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False
    )
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Webhook/verification secret (e.g. Evolution API token), Fernet-encrypted at rest.
    secret_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
