"""Add isolated local accounts and revocable server-side sessions."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013_password_auth"
down_revision = "0012_confluence_kb_sources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("documents", sa.Column("size_bytes", sa.Integer(), nullable=True))
    op.add_column("knowledge_bases", sa.Column("owner_subject", sa.String(255), nullable=True))
    op.create_index("ix_knowledge_bases_owner_subject", "knowledge_bases", ["owner_subject"])
    # Existing KBs remain unassigned: only the administrator sees them in password mode.
    # Existing workspaces, documents, vectors and conversations are never rewritten.
    op.create_table(
        "auth_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("username", sa.String(64), nullable=False, unique=True),
        sa.Column("subject", sa.String(255), nullable=False, unique=True),
        sa.Column("tenant_id", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("uq_auth_users_single_admin", "auth_users", ["is_admin"], unique=True,
                    postgresql_where=sa.text("is_admin IS TRUE"))
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("auth_users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_table(
        "auth_rate_limits",
        sa.Column("bucket_key", sa.String(64), primary_key=True),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    # Refuse to silently delete live accounts/sessions when rolling back a deployment.
    if op.get_bind().execute(sa.text("SELECT EXISTS (SELECT 1 FROM auth_users)")).scalar():
        raise RuntimeError("Cannot downgrade while local accounts exist")
    op.drop_table("auth_rate_limits")
    op.drop_table("auth_sessions")
    op.drop_table("auth_users")
    op.drop_index("ix_knowledge_bases_owner_subject", table_name="knowledge_bases")
    op.drop_column("knowledge_bases", "owner_subject")
    op.drop_column("documents", "size_bytes")
