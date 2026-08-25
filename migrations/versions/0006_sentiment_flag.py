"""sentiment_flag: per-agent opt-in for the sentiment escalation classifier

`agents.sentiment_escalation_enabled` gates whether the frustration
classifier (see app/agents/sentiment.py) runs before an agent turn. Not
nullable with a `false` server default so existing rows stay valid and
default off; the demo agent is flipped to `True` by `app/api/chat.py`'s
bootstrap. No enum change here — `handoff_requested_by` already contains
'system' (migration 0002).

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agents",
        sa.Column(
            "sentiment_escalation_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("agents", "sentiment_escalation_enabled")
