"""message_media: media_key/media_mime for image/audio messages

Ticket 26: a message with `type in ("image", "audio")` gets its stored file
key/mime recorded here — `media_key` is the `LocalMediaStorage` key
(`app/media/storage.py`), `media_mime` is the validated content-type. Both
NULL for text (and unsupported/tool) messages.

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-25

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("media_key", sa.String(255), nullable=True))
    op.add_column("messages", sa.Column("media_mime", sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "media_mime")
    op.drop_column("messages", "media_key")
