"""lookup indexes: handoffs by conversation, memories by contact, the operator inbox

`ix_handoffs_conversation_id_created_at` serves the handoff lookups of a turn,
`ix_memories_contact_id` the memories a contact gets, and
`ix_conversations_tenant_state_last_message_at` the inbox list.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-22

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_handoffs_conversation_id_created_at",
        "handoffs",
        ["conversation_id", "created_at"],
    )
    op.create_index("ix_memories_contact_id", "memories", ["contact_id"])
    op.create_index(
        "ix_conversations_tenant_state_last_message_at",
        "conversations",
        ["tenant_id", "state", "last_message_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_tenant_state_last_message_at", table_name="conversations")
    op.drop_index("ix_memories_contact_id", table_name="memories")
    op.drop_index("ix_handoffs_conversation_id_created_at", table_name="handoffs")
