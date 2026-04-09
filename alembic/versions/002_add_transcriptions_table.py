"""add_transcriptions_table

Revision ID: 002
Revises: 001
Create Date: 2026-04-09

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: Union[str, Sequence[str], None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "transcriptions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transcription_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "audio_file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "transcript_file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("files.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("language", sa.String(16), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_transcriptions_workspace_id", "transcriptions", ["workspace_id"])
    op.create_index("ix_transcriptions_task_id", "transcriptions", ["task_id"], unique=True)
    op.create_index("ix_transcriptions_audio_file_id", "transcriptions", ["audio_file_id"])
    op.create_index("ix_transcriptions_transcript_file_id", "transcriptions", ["transcript_file_id"])

    op.add_column(
        "transcription_tasks",
        sa.Column(
            "transcription_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transcriptions.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("transcription_tasks", "transcription_id")
    op.drop_index("ix_transcriptions_transcript_file_id", table_name="transcriptions")
    op.drop_index("ix_transcriptions_audio_file_id", table_name="transcriptions")
    op.drop_index("ix_transcriptions_task_id", table_name="transcriptions")
    op.drop_index("ix_transcriptions_workspace_id", table_name="transcriptions")
    op.drop_table("transcriptions")
