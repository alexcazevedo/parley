"""handoff_message: per-agent copy sent when a conversation flips to human handoff

`agents.handoff_message` is text with a server default so existing agent rows
(and any inserted before the ORM default kicks in) still get a sane message —
see app/tools/builtin.py's `handoff_to_human` and app/agents/runtime.py.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-24

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEFAULT_HANDOFF_MESSAGE = "Vou te transferir para um atendente humano."


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "handoff_message",
            sa.Text(),
            nullable=False,
            server_default=DEFAULT_HANDOFF_MESSAGE,
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "handoff_message")
