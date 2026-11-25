"""Add outbound connector control plane.

Revision ID: 0010_outbound_connector
Revises: 0009_reliable_exact_sources
"""

import sqlalchemy as sa

from alembic import op

revision = "0010_outbound_connector"
down_revision = "0009_reliable_exact_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "connectors",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("credential_hash", sa.String(length=64)),
        sa.Column("enrollment_hash", sa.String(length=64)),
        sa.Column("enrollment_expires_at", sa.DateTime(timezone=True)),
        sa.Column("allowed_spaces", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("capabilities", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("version", sa.String(length=100)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_connectors_tenant_id", "connectors", ["tenant_id"])
    op.create_index(
        "ix_connectors_tenant_last_seen",
        "connectors",
        ["tenant_id", "last_seen_at"],
    )

    op.create_table(
        "connector_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "connector_id",
            sa.Uuid(),
            sa.ForeignKey("connectors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(length=255), nullable=False),
        sa.Column(
            "workspace_id",
            sa.Uuid(),
            sa.ForeignKey("workspaces.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("requested_by", sa.String(length=255), nullable=False),
        sa.Column("type", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default=sa.text("'QUEUED'")),
        sa.Column("result", sa.JSON()),
        sa.Column("error", sa.Text()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_id", sa.Uuid()),
        sa.Column("leased_until", sa.DateTime(timezone=True)),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "type IN ('LIST_SPACES', 'LIST_CHILDREN', 'IMPORT_PAGES')",
            name="ck_connector_jobs_type",
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'LEASED', 'SUCCEEDED', 'FAILED', 'CANCELLED')",
            name="ck_connector_jobs_status",
        ),
    )
    op.create_index("ix_connector_jobs_tenant_id", "connector_jobs", ["tenant_id"])
    op.create_index(
        "ix_connector_jobs_claim",
        "connector_jobs",
        ["connector_id", "status", "created_at"],
    )
    op.create_index(
        "ix_connector_jobs_workspace",
        "connector_jobs",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_connector_jobs_workspace", table_name="connector_jobs")
    op.drop_index("ix_connector_jobs_claim", table_name="connector_jobs")
    op.drop_index("ix_connector_jobs_tenant_id", table_name="connector_jobs")
    op.drop_table("connector_jobs")
    op.drop_index("ix_connectors_tenant_last_seen", table_name="connectors")
    op.drop_index("ix_connectors_tenant_id", table_name="connectors")
    op.drop_table("connectors")
