from __future__ import annotations

import uuid

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings


class QueueError(RuntimeError):
    """Raised when an ingestion job cannot be queued."""


class IngestionQueue:
    def __init__(self) -> None:
        self._client = Redis.from_url(settings.redis_url, decode_responses=True)
        self._queue_name = settings.ingestion_queue

    async def enqueue(self, job_id: uuid.UUID) -> None:
        try:
            await self._client.rpush(self._queue_name, str(job_id))
        except RedisError as exc:
            raise QueueError("Failed to enqueue ingestion job") from exc

    async def close(self) -> None:
        await self._client.aclose()


ingestion_queue = IngestionQueue()
