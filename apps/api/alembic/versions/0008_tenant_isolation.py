"""Add tenant ownership boundaries to knowledge bases and workspaces.

Revision ID: 0008_tenant_isolation
Revises: 0007_embedding_dimension_384
"""

import sqlalchemy as sa

from alembic import op

revision = "0008_tenant_isolation"
down_revision = "0007_embedding_dimension_384"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("knowledge_bases", sa.Column("tenant_id", sa.String(255), nullable=True))
    op.add_column("workspaces", sa.Column("tenant_id", sa.String(255), nullable=True))
    op.execute("UPDATE knowledge_bases SET tenant_id = 'local' WHERE tenant_id IS NULL")
    op.execute("UPDATE workspaces SET tenant_id = 'local' WHERE tenant_id IS NULL")
    # Pre-auth user_id came from an untrusted request body. Normalize every
    # legacy workspace to the local principal so existing local data stays visible.
    op.execute("UPDATE workspaces SET user_id = 'local-user'")
    op.alter_column("knowledge_bases", "tenant_id", nullable=False)
    op.alter_column("workspaces", "tenant_id", nullable=False)
    op.alter_column("workspaces", "user_id", existing_type=sa.String(255), nullable=False)

    op.drop_constraint("knowledge_bases_slug_key", "knowledge_bases", type_="unique")
    op.create_unique_constraint(
        "uq_kb_tenant_slug",
        "knowledge_bases",
        ["tenant_id", "slug"],
    )
    op.create_index("ix_knowledge_bases_tenant_id", "knowledge_bases", ["tenant_id"])
    op.create_index("ix_workspaces_tenant_id", "workspaces", ["tenant_id"])
    op.create_index("ix_workspaces_user_id", "workspaces", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_workspaces_user_id", table_name="workspaces")
    op.drop_index("ix_workspaces_tenant_id", table_name="workspaces")
    op.drop_index("ix_knowledge_bases_tenant_id", table_name="knowledge_bases")
    op.drop_constraint("uq_kb_tenant_slug", "knowledge_bases", type_="unique")
    op.create_unique_constraint("knowledge_bases_slug_key", "knowledge_bases", ["slug"])
    op.alter_column("workspaces", "user_id", existing_type=sa.String(255), nullable=True)
    op.drop_column("workspaces", "tenant_id")
    op.drop_column("knowledge_bases", "tenant_id")
