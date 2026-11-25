from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import uuid

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings

logger = logging.getLogger(__name__)


def issue_bound_token(connector_id: uuid.UUID) -> tuple[str, str]:
    """Return a one-time plaintext token and the digest safe to persist."""

    secret = secrets.token_urlsafe(32)
    return f"{connector_id}.{secret}", hashlib.sha256(secret.encode()).hexdigest()


def token_connector_id(token: str) -> uuid.UUID | None:
    connector_id, separator, secret = token.partition(".")
    if not separator or not secret or len(secret) > 256:
        return None
    try:
        return uuid.UUID(connector_id)
    except ValueError:
        return None


def token_matches(token: str, connector_id: uuid.UUID, expected_digest: str | None) -> bool:
    parsed_id = token_connector_id(token)
    if parsed_id != connector_id or not expected_digest:
        return False
    secret = token.partition(".")[2]
    actual = hashlib.sha256(secret.encode()).hexdigest()
    return hmac.compare_digest(actual, expected_digest)


class ConnectorQueue:
    """Redis wake-ups for a queue whose authoritative state lives in PostgreSQL."""

    def __init__(self) -> None:
        self._client = Redis.from_url(
            settings.redis_url,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=settings.confluence_connector_long_poll_seconds + 5,
        )

    @staticmethod
    def _key(connector_id: uuid.UUID) -> str:
        return f"rag:connector:{connector_id}:wake"

    async def notify(self, connector_id: uuid.UUID, job_id: uuid.UUID) -> bool:
        try:
            key = self._key(connector_id)
            await self._client.rpush(key, str(job_id))
            await self._client.expire(key, settings.confluence_connector_job_timeout_seconds * 2)
        except RedisError:
            logger.warning(
                "Redis notification failed for connector job %s",
                job_id,
                exc_info=True,
            )
            return False
        return True

    async def wait(self, connector_id: uuid.UUID, seconds: int) -> None:
        try:
            await self._client.blpop(self._key(connector_id), timeout=seconds)
        except RedisError:
            logger.warning(
                "Redis long-poll wake-up failed for connector %s",
                connector_id,
                exc_info=True,
            )
            await asyncio.sleep(min(seconds, 2))

    async def close(self) -> None:
        await self._client.aclose()


connector_queue = ConnectorQueue()
