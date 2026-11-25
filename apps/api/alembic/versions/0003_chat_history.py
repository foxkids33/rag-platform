"""Add chat history ordering and indexes.

Revision ID: 0003_chat_history
Revises: 0002_document_search_enabled
"""
import sqlalchemy as sa

from alembic import op

revision = "0003_chat_history"
down_revision = "0002_document_search_enabled"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_sessions",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_chat_sessions_workspace_updated",
        "chat_sessions",
        ["workspace_id", "updated_at"],
    )
    op.create_index(
        "ix_chat_messages_session_created",
        "chat_messages",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_session_created", table_name="chat_messages")
    op.drop_index("ix_chat_sessions_workspace_updated", table_name="chat_sessions")
    op.drop_column("chat_sessions", "updated_at")
