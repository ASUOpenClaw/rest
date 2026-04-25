"""add_goclaw_session_key_to_conversations

Revision ID: 004
Revises: 003
Create Date: 2026-04-25

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "004"
down_revision: Union[str, Sequence[str], None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("goclaw_session_key", sa.String(255), nullable=True),
    )
    # Unique per workspace — one session key maps to exactly one conversation.
    op.create_index(
        "uq_conversations_ws_session_key",
        "conversations",
        ["workspace_id", "goclaw_session_key"],
        unique=True,
        postgresql_where=sa.text("goclaw_session_key IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_conversations_ws_session_key", table_name="conversations")
    op.drop_column("conversations", "goclaw_session_key")
