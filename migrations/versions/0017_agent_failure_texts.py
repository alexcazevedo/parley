"""agent failure texts: per-agent error and unsupported-media replies

`agents.error_message` is what a customer hears after a technical error,
`agents.unsupported_media_message` what they hear for a batch of media the
harness cannot handle. Null means the built-in default text.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-23

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("error_message", sa.Text(), nullable=True))
    op.add_column("agents", sa.Column("unsupported_media_message", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "unsupported_media_message")
    op.drop_column("agents", "error_message")
