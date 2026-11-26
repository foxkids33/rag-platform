"""Cache compacted chat history at immutable branch anchors."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0014_chat_memory"
down_revision = "0013_password_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "chat_history_checkpoints",
        sa.Column("message_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("chat_messages.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("scope", sa.String(16), primary_key=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.CheckConstraint("scope IN ('general', 'grounded')", name="ck_chat_history_checkpoint_scope"),
    )


def downgrade() -> None:
    op.drop_table("chat_history_checkpoints")
