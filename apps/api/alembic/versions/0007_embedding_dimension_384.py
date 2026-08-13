"""Store the native 384-dimensional MiniLM embeddings.

Revision ID: 0007_embedding_dimension_384
Revises: 0006_conversation_graph
"""

from alembic import op

revision = "0007_embedding_dimension_384"
down_revision = "0006_conversation_graph"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Remove the 640 trailing zeros added by the old embedding service."""

    op.drop_index("ix_chunks_embedding_hnsw", table_name="document_chunks")
    op.execute(
        """
        ALTER TABLE document_chunks
        ALTER COLUMN embedding TYPE vector(384)
        USING CASE
            WHEN embedding IS NULL THEN NULL
            ELSE subvector(embedding, 1, 384)::vector(384)
        END
        """
    )
    op.execute(
        """
        CREATE INDEX ix_chunks_embedding_hnsw ON document_chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )


def downgrade() -> None:
    """Restore the previous zero-padded representation."""

    op.drop_index("ix_chunks_embedding_hnsw", table_name="document_chunks")
    op.execute(
        """
        ALTER TABLE document_chunks
        ALTER COLUMN embedding TYPE vector(1024)
        USING CASE
            WHEN embedding IS NULL THEN NULL
            ELSE (
                embedding::real[] || array_fill(0.0::real, ARRAY[640])
            )::vector(1024)
        END
        """
    )
    op.execute(
        """
        CREATE INDEX ix_chunks_embedding_hnsw ON document_chunks
        USING hnsw (embedding vector_cosine_ops)
        WITH (m = 16, ef_construction = 64)
        """
    )
