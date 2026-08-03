"""Add document search visibility.

Revision ID: 0002_document_search_enabled
Revises: 0001_initial
"""
from alembic import op
import sqlalchemy as sa

revision = "0002_document_search_enabled"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column(
            "search_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("documents", "search_enabled")
