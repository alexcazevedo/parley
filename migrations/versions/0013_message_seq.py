"""message_seq: one order for messages, and a summary cursor on it

`messages.created_at` is stamped by the database for inbound rows and by the
app clock for the others, so it cannot order a conversation. This adds
`messages.seq` (`BIGINT NOT NULL DEFAULT nextval('messages_seq')`) and the
index `ix_messages_conversation_id_seq` on `(conversation_id, seq)`. Existing
rows are numbered in `(created_at, id)` order, the order every read used
until now, and the sequence continues after the highest number.

`conversations.summary_cursor` (a row count used as `OFFSET`) becomes
`conversations.summary_last_seq`: the `seq` of the row at old position
`summary_cursor` in `(created_at, id)` order (NULL when the cursor is 0 or
NULL; a cursor larger than the conversation takes its last row).

`ADD COLUMN` takes an `ACCESS EXCLUSIVE` lock on `messages`, held until the
migration's transaction ends, so no row is inserted between the numbering
and the `NOT NULL`. The migration rewrites every message row, and while it
runs both reads and writes of `messages` are blocked (webhooks, turns and the
operator view wait); its duration grows with the table. Run it in a quiet
period (docs/FOLLOW_UPS.md).

Downgrade restores `summary_cursor` as the count of the conversation's
messages with `seq <= summary_last_seq`, then drops the index, the columns
and the sequence.

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-19

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE SEQUENCE messages_seq AS BIGINT")
    op.add_column("messages", sa.Column("seq", sa.BigInteger(), nullable=True))
    op.execute(
        """
        UPDATE messages
        SET seq = numbered.position
        FROM (
            SELECT id, row_number() OVER (ORDER BY created_at, id) AS position
            FROM messages
        ) AS numbered
        WHERE messages.id = numbered.id
        """
    )
    # Next value: one past the highest number (1 on an empty table).
    op.execute(
        "SELECT setval('messages_seq', coalesce(max(seq), 0) + 1, false) FROM messages"
    )
    op.alter_column(
        "messages",
        "seq",
        server_default=sa.text("nextval('messages_seq')"),
        nullable=False,
    )
    op.execute("ALTER SEQUENCE messages_seq OWNED BY messages.seq")
    op.create_index("ix_messages_conversation_id_seq", "messages", ["conversation_id", "seq"])

    op.add_column(
        "conversations", sa.Column("summary_last_seq", sa.BigInteger(), nullable=True)
    )
    op.execute(
        """
        UPDATE conversations
        SET summary_last_seq = covered.last_seq
        FROM (
            SELECT ranked.conversation_id, max(ranked.seq) AS last_seq
            FROM (
                SELECT conversation_id, seq,
                       row_number() OVER (
                           PARTITION BY conversation_id ORDER BY created_at, id
                       ) AS position
                FROM messages
            ) AS ranked
            JOIN conversations AS c ON c.id = ranked.conversation_id
            WHERE c.summary_cursor > 0 AND ranked.position <= c.summary_cursor
            GROUP BY ranked.conversation_id
        ) AS covered
        WHERE conversations.id = covered.conversation_id
        """
    )
    op.drop_column("conversations", "summary_cursor")


def downgrade() -> None:
    op.add_column("conversations", sa.Column("summary_cursor", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE conversations
        SET summary_cursor = (
            SELECT count(*)
            FROM messages
            WHERE messages.conversation_id = conversations.id
              AND messages.seq <= conversations.summary_last_seq
        )
        WHERE summary_last_seq IS NOT NULL
        """
    )
    op.drop_column("conversations", "summary_last_seq")
    op.drop_index("ix_messages_conversation_id_seq", table_name="messages")
    # Dropping the owning column drops `messages_seq` with it.
    op.drop_column("messages", "seq")
    op.execute("DROP SEQUENCE IF EXISTS messages_seq")
