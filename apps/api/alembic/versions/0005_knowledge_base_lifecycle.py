"""Knowledge-base version lifecycle indexes.

Revision ID: 0005_knowledge_base_lifecycle
Revises: 0004_workspace_source_modes
"""

from alembic import op

revision = "0005_knowledge_base_lifecycle"
down_revision = "0004_workspace_source_modes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_documents_knowledge_base_version_id",
        "documents",
        ["knowledge_base_version_id"],
    )
    op.create_index(
        "ix_kb_versions_knowledge_base_status",
        "knowledge_base_versions",
        ["knowledge_base_id", "status"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_kb_versions_knowledge_base_status",
        table_name="knowledge_base_versions",
    )
    op.drop_index(
        "ix_documents_knowledge_base_version_id",
        table_name="documents",
    )
