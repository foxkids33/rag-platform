"""Add parent links for branching conversations.

Revision ID: 0006_conversation_graph
Revises: 0005_knowledge_base_lifecycle
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_conversation_graph"
down_revision = "0005_knowledge_base_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_messages",
        sa.Column("parent_message_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        """
        WITH ordered AS (
            SELECT
                id,
                role,
                lag(id) OVER (
                    PARTITION BY session_id
                    ORDER BY created_at ASC, CASE WHEN role = 'user' THEN 0 ELSE 1 END, id ASC
                ) AS previous_id,
                lag(role) OVER (
                    PARTITION BY session_id
                    ORDER BY created_at ASC, CASE WHEN role = 'user' THEN 0 ELSE 1 END, id ASC
                ) AS previous_role
            FROM chat_messages
        )
        UPDATE chat_messages AS message
        SET parent_message_id = CASE
            WHEN ordered.role = 'user' AND ordered.previous_role = 'assistant'
                THEN ordered.previous_id
            WHEN ordered.role = 'assistant' AND ordered.previous_role = 'user'
                THEN ordered.previous_id
            ELSE NULL
        END
        FROM ordered
        WHERE message.id = ordered.id
        """
    )
    op.create_foreign_key(
        "fk_chat_messages_parent_message_id",
        "chat_messages",
        "chat_messages",
        ["parent_message_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_chat_messages_session_parent",
        "chat_messages",
        ["session_id", "parent_message_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_chat_messages_session_parent", table_name="chat_messages")
    op.drop_constraint(
        "fk_chat_messages_parent_message_id",
        "chat_messages",
        type_="foreignkey",
    )
    op.drop_column("chat_messages", "parent_message_id")
