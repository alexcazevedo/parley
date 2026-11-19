"""one_open_conversation: at most one open conversation per contact and channel

Before the partial unique index `uq_conversations_open_contact_channel` on
`conversations (tenant_id, contact_id, channel_id) WHERE state <> 'CLOSED'`
can be created, existing duplicates are closed. Per `(tenant_id, contact_id,
channel_id)` with more than one open conversation, one is kept: a
`HUMAN_HANDOFF` one if there is any, otherwise the most recently active
(`coalesce(last_message_at, created_at)`, then `created_at`, then `id`, all
descending). The others become `CLOSED` and their unresolved handoffs get
`resolved_at = now()`. Nothing is deleted and no messages are moved.

The upgrade first takes `LOCK TABLE conversations IN SHARE ROW EXCLUSIVE
MODE`, held until the migration's transaction ends. Without it a running app
could insert a new duplicate open conversation between the closing of the
duplicates and the index creation, and `CREATE UNIQUE INDEX` would fail. The
mode blocks every write to `conversations` (and a second such lock) but not
reads, and conflicts with nothing the index build itself needs. The index is
built without `CONCURRENTLY` (which cannot run in a transaction), so on a large
table run this in a quiet period: writes wait for the whole migration.

Downgrade drops the index only: the duplicates closed by the upgrade stay
closed.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-18

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No writes to conversations until the index exists (see the docstring).
    op.execute("LOCK TABLE conversations IN SHARE ROW EXCLUSIVE MODE")
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   row_number() OVER (
                       PARTITION BY tenant_id, contact_id, channel_id
                       ORDER BY (state = 'HUMAN_HANDOFF'::conversation_state) DESC,
                                coalesce(last_message_at, created_at) DESC,
                                created_at DESC,
                                id DESC
                   ) AS rank
            FROM conversations
            WHERE state <> 'CLOSED'::conversation_state
        ),
        closed AS (
            UPDATE conversations
            SET state = 'CLOSED'::conversation_state
            FROM ranked
            WHERE conversations.id = ranked.id AND ranked.rank > 1
            RETURNING conversations.id
        )
        UPDATE handoffs
        SET resolved_at = now()
        FROM closed
        WHERE handoffs.conversation_id = closed.id AND handoffs.resolved_at IS NULL
        """
    )
    op.create_index(
        "uq_conversations_open_contact_channel",
        "conversations",
        ["tenant_id", "contact_id", "channel_id"],
        unique=True,
        postgresql_where=sa.text("state <> 'CLOSED'::conversation_state"),
    )


def downgrade() -> None:
    # Duplicates closed by the upgrade stay closed.
    op.drop_index("uq_conversations_open_contact_channel", table_name="conversations")
