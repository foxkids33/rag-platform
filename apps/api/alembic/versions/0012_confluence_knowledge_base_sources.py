"""Preserve Confluence provenance in versioned knowledge-base snapshots."""

import sqlalchemy as sa

from alembic import op

revision = "0012_confluence_kb_sources"
down_revision = "0011_connector_space_pages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_documents_confluence_identity", "documents", type_="check")
    op.create_check_constraint(
        "ck_documents_confluence_identity",
        "documents",
        "source_type <> 'CONFLUENCE' OR "
        "((workspace_id IS NOT NULL OR knowledge_base_version_id IS NOT NULL) "
        "AND external_id IS NOT NULL)",
    )
    # Exactly-one-scope remains mandatory. Each version independently retains
    # one imported identity per source; workspace uniqueness is unchanged.
    op.create_index(
        "uq_documents_kb_external_source",
        "documents",
        ["knowledge_base_version_id", "source_type", "external_id"],
        unique=True,
        postgresql_where=sa.text(
            "knowledge_base_version_id IS NOT NULL AND external_id IS NOT NULL"
        ),
    )


def downgrade() -> None:
    # Never discard imported sources or relabel them as anonymous uploads.
    existing = op.get_bind().execute(sa.text(
        "SELECT EXISTS (SELECT 1 FROM documents WHERE source_type = 'CONFLUENCE' "
        "AND knowledge_base_version_id IS NOT NULL)"
    )).scalar()
    if existing:
        raise RuntimeError(
            "Cannot downgrade while knowledge-base Confluence snapshots exist"
        )
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index("uq_documents_kb_external_source", table_name="documents")
    op.drop_constraint("ck_documents_confluence_identity", "documents", type_="check")
    op.create_check_constraint(
        "ck_documents_confluence_identity",
        "documents",
        "source_type <> 'CONFLUENCE' OR "
        "(workspace_id IS NOT NULL AND external_id IS NOT NULL)",
    )
