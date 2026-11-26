"""tool_ledger: tool calls are recorded before they run

A `ToolExecution` row is committed with status `started` before the tool runs
and completed when it returns (see app/tools/ledger.py); `model_content` keeps
the exact string the model received. `agent_runs.input_message_ids` lists the
inbound messages a run answers. `tool_executions.terminal_text` keeps a
terminal result's text (`handoff_to_human`) so a reused call still ends the
loop. `ix_agent_runs_conversation_id` and `ix_tool_executions_agent_run_id`
serve `ledger.find_prior`'s filter and join. `ALTER TYPE ... ADD VALUE` cannot run inside a
transaction block, hence the autocommit block (as in 0007).

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-18

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE tool_execution_status ADD VALUE IF NOT EXISTS 'started'")
    op.add_column("tool_executions", sa.Column("model_content", sa.Text(), nullable=True))
    op.add_column("tool_executions", sa.Column("terminal_text", sa.Text(), nullable=True))
    op.add_column(
        "agent_runs",
        sa.Column("input_message_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_index("ix_agent_runs_conversation_id", "agent_runs", ["conversation_id"])
    op.create_index("ix_tool_executions_agent_run_id", "tool_executions", ["agent_run_id"])


def downgrade() -> None:
    op.drop_index("ix_tool_executions_agent_run_id", table_name="tool_executions")
    op.drop_index("ix_agent_runs_conversation_id", table_name="agent_runs")
    op.drop_column("agent_runs", "input_message_ids")
    op.drop_column("tool_executions", "terminal_text")
    op.drop_column("tool_executions", "model_content")
    # The 'started' enum value stays: PostgreSQL cannot drop a value from an
    # enum type (see 0007).
