"""Initial platform schema.

Revision ID: 0001_initial
Revises:
"""
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "knowledge_bases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(120), nullable=False, unique=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("active_version_id", sa.Uuid(), nullable=True),
        sa.Column("graph_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "knowledge_base_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("knowledge_base_id", sa.Uuid(), sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("embedding_model", sa.String(255), nullable=False),
        sa.Column("embedding_dimension", sa.Integer(), nullable=False),
        sa.Column("chunker_version", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("activated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("knowledge_base_id", "version", name="uq_kb_version"),
    )

    op.create_foreign_key(
        "fk_kb_active_version",
        "knowledge_bases",
        "knowledge_base_versions",
        ["active_version_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=True),
        sa.Column("base_knowledge_base_id", sa.Uuid(), sa.ForeignKey("knowledge_bases.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("knowledge_base_version_id", sa.Uuid(), sa.ForeignKey("knowledge_base_versions.id", ondelete="CASCADE")),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id", ondelete="CASCADE")),
        sa.Column("filename", sa.String(500), nullable=False),
        sa.Column("object_key", sa.String(1000), nullable=False),
        sa.Column("mime_type", sa.String(255)),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("title", sa.String(500)),
        sa.Column("source_url", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "((knowledge_base_version_id IS NOT NULL)::int + (workspace_id IS NOT NULL)::int) = 1",
            name="ck_document_exactly_one_scope",
        ),
    )

    op.create_table(
        "document_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("parent_chunk_id", sa.Uuid(), nullable=True),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("parent_text", sa.Text()),
        sa.Column("heading", sa.String(1000)),
        sa.Column("heading_breadcrumb", sa.Text()),
        sa.Column("page_start", sa.Integer()),
        sa.Column("page_end", sa.Integer()),
        sa.Column("embedding", Vector(1024)),
        sa.Column("search_vector", postgresql.TSVECTOR()),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("document_id", "chunk_index", name="uq_document_chunk_index"),
    )
    op.create_index("ix_chunks_document_id", "document_chunks", ["document_id"])
    op.execute("CREATE INDEX ix_chunks_search_vector ON document_chunks USING GIN (search_vector)")
    op.execute(
        "CREATE INDEX ix_chunks_embedding_hnsw ON document_chunks "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )

    op.create_table(
        "chat_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("session_id", sa.Uuid(), sa.ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("document_id", sa.Uuid(), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("ingestion_jobs")
    op.drop_table("chat_messages")
    op.drop_table("chat_sessions")
    op.drop_index("ix_chunks_document_id", table_name="document_chunks")
    op.drop_table("document_chunks")
    op.drop_table("documents")
    op.drop_table("workspaces")
    op.drop_constraint("fk_kb_active_version", "knowledge_bases", type_="foreignkey")
    op.drop_table("knowledge_base_versions")
    op.drop_table("knowledge_bases")
