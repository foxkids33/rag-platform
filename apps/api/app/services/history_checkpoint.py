"""Storage for branch-scoped summaries. The calling chat request owns the transaction."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


async def latest_checkpoint(
    db: AsyncSession, conversation_id: UUID, candidate_ids: Sequence[UUID], scope: str,
) -> tuple[UUID, str] | None:
    """Return the last stored anchor in the supplied oldest-to-newest ancestry."""
    if not candidate_ids:
        return None
    result = await db.execute(
        text("""
            SELECT c.message_id, c.summary
            FROM unnest(CAST(:ids AS uuid[])) WITH ORDINALITY AS a(id, position)
            JOIN chat_history_checkpoints c ON c.message_id = a.id
            JOIN chat_messages m ON m.id = c.message_id
            WHERE m.session_id = :conversation_id AND c.scope = :scope
            ORDER BY a.position DESC
            LIMIT 1
        """),
        {"ids": list(candidate_ids), "conversation_id": conversation_id, "scope": scope},
    )
    row = result.one_or_none()
    return (row.message_id, row.summary) if row else None


async def save_checkpoint(
    db: AsyncSession, conversation_id: UUID, covered_message_id: UUID, scope: str, summary: str,
) -> str | None:
    """Publish an immutable checkpoint. Skip an anchor locked by deletion or another writer."""
    result = await db.execute(
        text("""
            WITH anchor AS (
                SELECT id FROM chat_messages
                WHERE id = :message_id AND session_id = :conversation_id
                FOR KEY SHARE SKIP LOCKED
            )
            INSERT INTO chat_history_checkpoints (message_id, scope, summary)
            SELECT id, CAST(:scope AS text), :summary FROM anchor
            WHERE pg_try_advisory_xact_lock(
                hashtextextended('chat-history:' || CAST(id AS text) || ':' || CAST(:scope AS text), 0)
            )
            ON CONFLICT (message_id, scope) DO NOTHING
            RETURNING summary
        """),
        {"message_id": covered_message_id, "conversation_id": conversation_id,
         "scope": scope, "summary": summary},
    )
    stored = result.scalar_one_or_none()
    if stored is not None:
        return stored
    # A committed winner is reusable. An in-flight writer can finish independently.
    existing = await latest_checkpoint(db, conversation_id, [covered_message_id], scope)
    return existing[1] if existing else None
