"""Add durable jobs, exact-match text and external document metadata.

Revision ID: 0009_reliable_exact_sources
Revises: 0008_tenant_isolation
"""

import sqlalchemy as sa

from alembic import op

revision = "0009_reliable_exact_sources"
down_revision = "0008_tenant_isolation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.add_column(
        "document_chunks",
        sa.Column("normalized_text", sa.Text(), nullable=True),
    )
    op.execute(
        """
        UPDATE document_chunks
        SET normalized_text = regexp_replace(
            lower(
                translate(
                    concat_ws(' ', heading_breadcrumb, heading, text),
                    'Ёё',
                    'Ее'
                )
            ),
            '[^0-9a-zа-я]+',
            '',
            'g'
        )
        """
    )
    op.alter_column(
        "document_chunks",
        "normalized_text",
        existing_type=sa.Text(),
        nullable=False,
        server_default=sa.text("''"),
    )
    op.execute(
        "CREATE INDEX ix_chunks_normalized_text_trgm ON document_chunks "
        "USING gin (normalized_text gin_trgm_ops)"
    )

    op.add_column(
        "documents",
        sa.Column(
            "source_type",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'UPLOAD'"),
        ),
    )
    op.add_column("documents", sa.Column("external_id", sa.String(length=255)))
    op.add_column("documents", sa.Column("external_version", sa.String(length=255)))
    op.add_column("documents", sa.Column("synced_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_documents_source_type",
        "documents",
        "source_type IN ('UPLOAD', 'CONFLUENCE')",
    )
    op.create_check_constraint(
        "ck_documents_confluence_identity",
        "documents",
        "source_type <> 'CONFLUENCE' OR "
        "(workspace_id IS NOT NULL AND external_id IS NOT NULL)",
    )
    op.create_index(
        "uq_documents_workspace_external_source",
        "documents",
        ["workspace_id", "source_type", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
    op.create_index(
        "ix_documents_workspace_sha256",
        "documents",
        ["workspace_id", "sha256"],
        postgresql_where=sa.text("workspace_id IS NOT NULL"),
    )

    op.add_column(
        "ingestion_jobs",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("ingestion_jobs", sa.Column("lease_id", sa.Uuid()))
    op.create_index(
        "ix_ingestion_jobs_claim",
        "ingestion_jobs",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_ingestion_jobs_stale",
        "ingestion_jobs",
        ["status", "updated_at"],
    )

    op.create_table(
        "object_deletion_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("object_key", sa.String(length=1000), nullable=False, unique=True),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default=sa.text("'QUEUED'"),
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_id", sa.Uuid()),
        sa.Column("error", sa.Text()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('QUEUED', 'PROCESSING', 'FAILED')",
            name="ck_object_deletion_jobs_status",
        ),
    )
    op.create_index(
        "ix_object_deletion_jobs_claim",
        "object_deletion_jobs",
        ["status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_object_deletion_jobs_claim", table_name="object_deletion_jobs")
    op.drop_table("object_deletion_jobs")

    op.drop_index("ix_ingestion_jobs_stale", table_name="ingestion_jobs")
    op.drop_index("ix_ingestion_jobs_claim", table_name="ingestion_jobs")
    op.drop_column("ingestion_jobs", "lease_id")
    op.drop_column("ingestion_jobs", "attempts")

    op.drop_index("ix_documents_workspace_sha256", table_name="documents")
    op.drop_index("uq_documents_workspace_external_source", table_name="documents")
    op.execute(
        "ALTER TABLE documents "
        "DROP CONSTRAINT IF EXISTS ck_documents_confluence_identity"
    )
    op.drop_constraint("ck_documents_source_type", "documents", type_="check")
    op.drop_column("documents", "synced_at")
    op.drop_column("documents", "external_version")
    op.drop_column("documents", "external_id")
    op.drop_column("documents", "source_type")

    op.drop_index("ix_chunks_normalized_text_trgm", table_name="document_chunks")
    op.drop_column("document_chunks", "normalized_text")
