from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from contextlib import suppress

from anyio import to_thread
from sqlalchemy import delete, func
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ObjectDeletionJob
from app.services.storage import StorageError, storage

logger = logging.getLogger(__name__)


async def schedule_object_deletions(
    db: AsyncSession,
    object_keys: Iterable[str],
) -> list[str]:
    """Persist object removals in the same transaction as metadata deletion."""

    keys = list(dict.fromkeys(key for key in object_keys if key))
    if not keys:
        return []
    statement = insert(ObjectDeletionJob).values(
        [
            {
                "id": uuid.uuid4(),
                "object_key": key,
                "status": "QUEUED",
                "attempts": 0,
            }
            for key in keys
        ]
    )
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=["object_key"],
            set_={
                "status": "QUEUED",
                "attempts": 0,
                "lease_id": None,
                "error": None,
                "updated_at": func.now(),
            },
            where=ObjectDeletionJob.status == "FAILED",
        )
    )
    return keys


async def try_delete_scheduled_objects(
    db: AsyncSession,
    object_keys: Iterable[str],
) -> None:
    """Fast-path an outbox batch; the worker retains and retries failures."""

    deleted: list[str] = []
    for object_key in object_keys:
        try:
            await to_thread.run_sync(storage.delete, object_key)
        except StorageError:
            logger.warning(
                "Object cleanup deferred to worker for %s",
                object_key,
                exc_info=True,
            )
        else:
            deleted.append(object_key)
    if deleted:
        try:
            await db.execute(
                delete(ObjectDeletionJob).where(ObjectDeletionJob.object_key.in_(deleted))
            )
            await db.commit()
        except SQLAlchemyError:
            # The durable row deliberately remains safe to retry. S3 deletion is
            # idempotent, so a later worker attempt cannot corrupt live data.
            with suppress(SQLAlchemyError):
                await db.rollback()
            logger.warning(
                "Failed to acknowledge fast-path object cleanup; worker will retry",
                exc_info=True,
            )
