from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from contextlib import suppress

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import settings
from app.document_processing import ParsedChunk, parse_document
from app.embeddings import embeddings
from app.storage import storage
from app.text_processing import normalize_exact_text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("rag-worker")

engine: AsyncEngine = create_async_engine(settings.database_url, pool_pre_ping=True)
ZERO_UUID = uuid.UUID(int=0)


async def _claim_next_job(preferred_job_id: uuid.UUID | None = None) -> dict | None:
    """Claim a queued DB job; Redis only influences which job is tried first."""

    lease_id = uuid.uuid4()
    async with engine.begin() as connection:
        result = await connection.execute(
            text(
                """
                SELECT
                    j.id AS job_id,
                    j.document_id,
                    j.attempts + 1 AS attempts,
                    d.object_key,
                    d.filename
                FROM ingestion_jobs AS j
                JOIN documents AS d ON d.id = j.document_id
                WHERE j.status = 'QUEUED'
                  AND j.attempts < :max_attempts
                ORDER BY
                    CASE WHEN j.id = :preferred_job_id THEN 0 ELSE 1 END,
                    j.created_at,
                    j.id
                FOR UPDATE OF j, d SKIP LOCKED
                LIMIT 1
                """
            ),
            {
                "max_attempts": settings.ingestion_max_attempts,
                "preferred_job_id": preferred_job_id or ZERO_UUID,
            },
        )
        row = result.mappings().first()
        if row is None:
            return None

        await connection.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET status = 'PROCESSING',
                    progress = 10,
                    attempts = attempts + 1,
                    lease_id = :lease_id,
                    error = NULL,
                    updated_at = now()
                WHERE id = :job_id
                """
            ),
            {"job_id": row["job_id"], "lease_id": lease_id},
        )
        await connection.execute(
            text("UPDATE documents SET status = 'PROCESSING' WHERE id = :document_id"),
            {"document_id": row["document_id"]},
        )
        job = dict(row)
        job["lease_id"] = lease_id
        return job


async def _claim_job(job_id: uuid.UUID) -> dict | None:
    """Claim only the explicitly requested job (used by tests and operators)."""

    lease_id = uuid.uuid4()
    async with engine.begin() as connection:
        result = await connection.execute(
            text(
                """
                SELECT
                    j.id AS job_id,
                    j.document_id,
                    j.attempts + 1 AS attempts,
                    d.object_key,
                    d.filename
                FROM ingestion_jobs AS j
                JOIN documents AS d ON d.id = j.document_id
                WHERE j.id = :job_id
                  AND j.status = 'QUEUED'
                  AND j.attempts < :max_attempts
                FOR UPDATE OF j, d SKIP LOCKED
                """
            ),
            {"job_id": job_id, "max_attempts": settings.ingestion_max_attempts},
        )
        row = result.mappings().first()
        if row is None:
            return None
        await connection.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET status = 'PROCESSING', progress = 10, attempts = attempts + 1,
                    lease_id = :lease_id,
                    error = NULL, updated_at = now()
                WHERE id = :job_id
                """
            ),
            {"job_id": job_id, "lease_id": lease_id},
        )
        await connection.execute(
            text("UPDATE documents SET status = 'PROCESSING' WHERE id = :document_id"),
            {"document_id": row["document_id"]},
        )
        job = dict(row)
        job["lease_id"] = lease_id
        return job


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.9g}" for value in vector) + "]"


async def _heartbeat(job_id: uuid.UUID, lease_id: uuid.UUID) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET updated_at = now()
                WHERE id = :job_id AND status = 'PROCESSING' AND lease_id = :lease_id
                """
            ),
            {"job_id": job_id, "lease_id": lease_id},
        )


async def _heartbeat_loop(
    job_id: uuid.UUID,
    lease_id: uuid.UUID,
    stopped: asyncio.Event,
) -> None:
    while not stopped.is_set():
        try:
            await asyncio.wait_for(
                stopped.wait(),
                timeout=settings.ingestion_heartbeat_seconds,
            )
        except TimeoutError:
            try:
                await _heartbeat(job_id, lease_id)
            except Exception:
                logger.exception("Failed to heartbeat ingestion job %s", job_id)


async def _store_chunks(
    job_id: uuid.UUID,
    lease_id: uuid.UUID,
    document_id: uuid.UUID,
    chunks: list[ParsedChunk],
    vectors: list[list[float]],
) -> None:
    if len(chunks) != len(vectors):
        raise ValueError("Chunk and embedding counts do not match")

    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM document_chunks WHERE document_id = :document_id"),
            {"document_id": document_id},
        )

        for index, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True)):
            heading_text = " ".join(
                value for value in (chunk.heading_breadcrumb, chunk.heading) if value
            )
            await connection.execute(
                text(
                    """
                    INSERT INTO document_chunks (
                        id, document_id, parent_chunk_id, chunk_index, text,
                        normalized_text, parent_text, heading, heading_breadcrumb,
                        page_start, page_end, embedding, search_vector, metadata
                    ) VALUES (
                        :id, :document_id, :parent_chunk_id, :chunk_index, :text,
                        :normalized_text, :parent_text, :heading, :heading_breadcrumb,
                        :page_start, :page_end, CAST(:embedding AS vector),
                        (
                            setweight(to_tsvector('russian', coalesce(:heading_text, '')), 'A')
                            || setweight(to_tsvector('russian', :search_text), 'B')
                            || setweight(to_tsvector('simple', :search_text), 'C')
                        ),
                        CAST(:metadata AS json)
                    )
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "document_id": document_id,
                    "parent_chunk_id": chunk.parent_chunk_id,
                    "chunk_index": index,
                    "text": chunk.text,
                    "normalized_text": normalize_exact_text(
                        f"{heading_text} {chunk.search_text}"
                    ),
                    "parent_text": chunk.parent_text,
                    "heading": chunk.heading,
                    "heading_breadcrumb": chunk.heading_breadcrumb,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "embedding": _vector_literal(vector),
                    "search_text": chunk.search_text,
                    "heading_text": heading_text,
                    "metadata": json.dumps(chunk.metadata, ensure_ascii=False),
                },
            )

        completed = await connection.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET status = 'READY', progress = 100, lease_id = NULL,
                    error = NULL, updated_at = now()
                WHERE id = :job_id AND status = 'PROCESSING' AND lease_id = :lease_id
                """
            ),
            {"job_id": job_id, "lease_id": lease_id},
        )
        if completed.rowcount != 1:
            raise RuntimeError("Ingestion job lost its processing lease")
        await connection.execute(
            text("UPDATE documents SET status = 'READY' WHERE id = :document_id"),
            {"document_id": document_id},
        )


async def _record_processing_error(
    job_id: uuid.UUID,
    lease_id: uuid.UUID,
    document_id: uuid.UUID,
    error: Exception,
) -> bool:
    """Record a failure and return True when PostgreSQL scheduled a retry."""

    message = str(error)[:4000] or error.__class__.__name__
    async with engine.begin() as connection:
        attempts = await connection.scalar(
            text(
                "SELECT attempts FROM ingestion_jobs "
                "WHERE id = :job_id AND lease_id = :lease_id FOR UPDATE"
            ),
            {"job_id": job_id, "lease_id": lease_id},
        )
        if attempts is None:
            return False
        retry = attempts is not None and int(attempts) < settings.ingestion_max_attempts
        next_status = "QUEUED" if retry else "FAILED"
        await connection.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET status = :status, progress = 0, lease_id = NULL,
                    error = :error, updated_at = now()
                WHERE id = :job_id AND lease_id = :lease_id
                """
            ),
            {
                "job_id": job_id,
                "lease_id": lease_id,
                "status": next_status,
                "error": message,
            },
        )
        await connection.execute(
            text("UPDATE documents SET status = :status WHERE id = :document_id"),
            {"document_id": document_id, "status": next_status},
        )
        return retry


async def _process_claimed_job(job: dict) -> None:
    job_id = job["job_id"]
    lease_id = job["lease_id"]
    document_id = job["document_id"]
    stopped = asyncio.Event()
    heartbeat_task = asyncio.create_task(_heartbeat_loop(job_id, lease_id, stopped))
    try:
        data = await asyncio.to_thread(storage.download, job["object_key"])
        chunks = await asyncio.to_thread(parse_document, job["filename"], data)
        if not chunks:
            raise ValueError("Document produced no chunks")

        vectors = await embeddings.embed([chunk.search_text for chunk in chunks])
        await _store_chunks(job_id, lease_id, document_id, chunks, vectors)
        logger.info("Document %s indexed into %d chunks", document_id, len(chunks))
    except Exception as exc:
        logger.exception("Ingestion job %s failed on attempt %s", job_id, job["attempts"])
        retry = await _record_processing_error(job_id, lease_id, document_id, exc)
        if retry:
            logger.warning("Ingestion job %s returned to the durable queue", job_id)
    finally:
        stopped.set()
        with suppress(asyncio.CancelledError):
            await heartbeat_task


async def process_job(job_id: uuid.UUID) -> None:
    job = await _claim_job(job_id)
    if job is None:
        logger.info("Skipping unavailable or already claimed job %s", job_id)
        return
    await _process_claimed_job(job)


async def _recover_stale_jobs() -> int:
    recovered = 0
    async with engine.begin() as connection:
        result = await connection.execute(
            text(
                """
                SELECT id, document_id, attempts
                FROM ingestion_jobs
                WHERE status = 'PROCESSING'
                  AND updated_at < now() - make_interval(secs => :stale_seconds)
                FOR UPDATE SKIP LOCKED
                """
            ),
            {"stale_seconds": settings.ingestion_stale_after_seconds},
        )
        for row in result.mappings():
            exhausted = row["attempts"] >= settings.ingestion_max_attempts
            next_status = "FAILED" if exhausted else "QUEUED"
            error = (
                "Ingestion worker lease expired after the final attempt"
                if exhausted
                else "Ingestion worker lease expired; retrying"
            )
            await connection.execute(
                text(
                    """
                    UPDATE ingestion_jobs
                    SET status = :status, progress = 0, lease_id = NULL,
                        error = :error, updated_at = now()
                    WHERE id = :job_id
                    """
                ),
                {"job_id": row["id"], "status": next_status, "error": error},
            )
            await connection.execute(
                text("UPDATE documents SET status = :status WHERE id = :document_id"),
                {"document_id": row["document_id"], "status": next_status},
            )
            recovered += 1
    return recovered


async def _claim_object_deletion() -> dict | None:
    lease_id = uuid.uuid4()
    async with engine.begin() as connection:
        result = await connection.execute(
            text(
                """
                SELECT id, object_key, attempts + 1 AS attempts
                FROM object_deletion_jobs
                WHERE status = 'QUEUED'
                  AND attempts < :max_attempts
                ORDER BY created_at, id
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            ),
            {"max_attempts": settings.object_deletion_max_attempts},
        )
        row = result.mappings().first()
        if row is None:
            return None
        await connection.execute(
            text(
                """
                UPDATE object_deletion_jobs
                SET status = 'PROCESSING', attempts = attempts + 1,
                    lease_id = :lease_id, updated_at = now()
                WHERE id = :job_id
                """
            ),
            {"job_id": row["id"], "lease_id": lease_id},
        )
        job = dict(row)
        job["lease_id"] = lease_id
        return job


async def _process_object_deletion() -> bool:
    job = await _claim_object_deletion()
    if job is None:
        return False
    try:
        await asyncio.to_thread(storage.delete, job["object_key"])
    except Exception as exc:
        message = str(exc)[:4000] or exc.__class__.__name__
        next_status = (
            "FAILED"
            if job["attempts"] >= settings.object_deletion_max_attempts
            else "QUEUED"
        )
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    UPDATE object_deletion_jobs
                    SET status = :status, lease_id = NULL,
                        error = :error, updated_at = now()
                    WHERE id = :job_id AND lease_id = :lease_id
                    """
                ),
                {
                    "job_id": job["id"],
                    "lease_id": job["lease_id"],
                    "status": next_status,
                    "error": message,
                },
            )
        logger.exception("Failed to delete object %s", job["object_key"])
    else:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "DELETE FROM object_deletion_jobs "
                    "WHERE id = :job_id AND lease_id = :lease_id"
                ),
                {"job_id": job["id"], "lease_id": job["lease_id"]},
            )
        logger.info("Deleted retired object %s", job["object_key"])
    return True


async def _recover_stale_object_deletions() -> int:
    async with engine.begin() as connection:
        result = await connection.execute(
            text(
                """
                UPDATE object_deletion_jobs
                SET status = CASE
                        WHEN attempts >= :max_attempts THEN 'FAILED'
                        ELSE 'QUEUED'
                    END,
                    lease_id = NULL,
                    error = 'Object deletion worker lease expired',
                    updated_at = now()
                WHERE status = 'PROCESSING'
                  AND updated_at < now() - make_interval(secs => :stale_seconds)
                """
            ),
            {
                "max_attempts": settings.object_deletion_max_attempts,
                "stale_seconds": settings.ingestion_stale_after_seconds,
            },
        )
        return result.rowcount


def _parse_notification(item: tuple[str, str] | None) -> uuid.UUID | None:
    if item is None:
        return None
    try:
        return uuid.UUID(item[1])
    except ValueError:
        logger.error("Ignoring malformed job id: %s", item[1])
        return None


async def main() -> None:
    redis = Redis.from_url(
        settings.redis_url,
        decode_responses=True,
        socket_connect_timeout=settings.ingestion_poll_seconds,
        socket_timeout=settings.ingestion_poll_seconds + 1,
    )
    logger.info(
        "RAG worker started; PostgreSQL queue is authoritative, Redis channel=%s",
        settings.ingestion_queue,
    )
    next_recovery = 0.0

    try:
        while True:
            try:
                now = time.monotonic()
                if now >= next_recovery:
                    recovered = await _recover_stale_jobs()
                    recovered += await _recover_stale_object_deletions()
                    if recovered:
                        logger.warning("Recovered %d stale background jobs", recovered)
                    next_recovery = now + settings.ingestion_recovery_interval_seconds

                preferred_job_id: uuid.UUID | None = None
                try:
                    preferred_job_id = _parse_notification(
                        await redis.blpop(
                            settings.ingestion_queue,
                            timeout=max(1, int(settings.ingestion_poll_seconds)),
                        )
                    )
                except RedisError:
                    logger.warning(
                        "Redis notification channel is unavailable; polling PostgreSQL"
                    )

                job = await _claim_next_job(preferred_job_id)
                if job is not None:
                    await _process_claimed_job(job)
                    continue
                if await _process_object_deletion():
                    continue
                await asyncio.sleep(settings.ingestion_poll_seconds)
            except SQLAlchemyError:
                logger.exception("PostgreSQL worker loop failed; retrying")
                await asyncio.sleep(settings.ingestion_poll_seconds)
    finally:
        await redis.aclose()
        await embeddings.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
