"""Allow paginated whole-space discovery through outbound connectors."""

from alembic import op

revision = "0011_connector_space_pages"
down_revision = "0010_outbound_connector"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_connector_jobs_type", "connector_jobs", type_="check")
    op.create_check_constraint(
        "ck_connector_jobs_type", "connector_jobs",
        "type IN ('LIST_SPACES', 'LIST_CHILDREN', 'LIST_SPACE_PAGES', 'IMPORT_PAGES')",
    )


def downgrade() -> None:
    # Preserve jobs: fail rather than silently delete incompatible history.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_constraint("ck_connector_jobs_type", "connector_jobs", type_="check")
    op.create_check_constraint(
        "ck_connector_jobs_type", "connector_jobs",
        "type IN ('LIST_SPACES', 'LIST_CHILDREN', 'IMPORT_PAGES')",
    )
