"""Add workspace source modes and update timestamps.

Revision ID: 0004_workspace_source_modes
Revises: 0003_chat_history
"""
import sqlalchemy as sa

from alembic import op

revision = "0004_workspace_source_modes"
down_revision = "0003_chat_history"
branch_labels = None
depends_on = None


SOURCE_MODE_CHECK = (
    "source_mode IN ('USER_DOCUMENTS', 'KNOWLEDGE_BASE', 'HYBRID')"
)


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column("source_mode", sa.String(length=32), nullable=True),
    )
    op.execute(
        """
        UPDATE workspaces
        SET source_mode = CASE
            WHEN base_knowledge_base_id IS NULL THEN 'USER_DOCUMENTS'
            ELSE 'HYBRID'
        END
        """
    )
    op.alter_column(
        "workspaces",
        "source_mode",
        existing_type=sa.String(length=32),
        nullable=False,
        server_default=sa.text("'USER_DOCUMENTS'"),
    )
    op.create_check_constraint(
        "ck_workspaces_source_mode",
        "workspaces",
        SOURCE_MODE_CHECK,
    )
    op.add_column(
        "workspaces",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "updated_at")
    op.drop_constraint("ck_workspaces_source_mode", "workspaces", type_="check")
    op.drop_column("workspaces", "source_mode")
