from __future__ import annotations

import asyncio
import json
import logging
import uuid

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.config import settings
from app.document_processing import ParsedChunk, parse_document
from app.embeddings import embeddings
from app.storage import storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("rag-worker")

engine: AsyncEngine = create_async_engine(settings.database_url, pool_pre_ping=True)


async def _claim_job(job_id: uuid.UUID) -> dict | None:
    async with engine.begin() as connection:
        result = await connection.execute(
            text(
                """
                SELECT j.id AS job_id, j.document_id, d.object_key, d.filename
                FROM ingestion_jobs AS j
                JOIN documents AS d ON d.id = j.document_id
                WHERE j.id = :job_id AND j.status = 'QUEUED'
                FOR UPDATE OF j, d SKIP LOCKED
                """
            ),
            {"job_id": job_id},
        )
        row = result.mappings().first()
        if row is None:
            return None

        await connection.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET status = 'PROCESSING', progress = 10, error = NULL, updated_at = now()
                WHERE id = :job_id
                """
            ),
            {"job_id": job_id},
        )
        await connection.execute(
            text("UPDATE documents SET status = 'PROCESSING' WHERE id = :document_id"),
            {"document_id": row["document_id"]},
        )
        return dict(row)


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.9g}" for value in vector) + "]"


async def _store_chunks(
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
            await connection.execute(
                text(
                    """
                    INSERT INTO document_chunks (
                        id, document_id, parent_chunk_id, chunk_index, text, parent_text,
                        heading, heading_breadcrumb, page_start, page_end,
                        embedding, search_vector, metadata
                    ) VALUES (
                        :id, :document_id, :parent_chunk_id, :chunk_index, :text, :parent_text,
                        :heading, :heading_breadcrumb, :page_start, :page_end,
                        CAST(:embedding AS vector),
                        (
                            setweight(
                                to_tsvector('russian', coalesce(:heading_text, '')),
                                'A'
                            )
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
                    "parent_text": chunk.parent_text,
                    "heading": chunk.heading,
                    "heading_breadcrumb": chunk.heading_breadcrumb,
                    "page_start": chunk.page_start,
                    "page_end": chunk.page_end,
                    "embedding": _vector_literal(vector),
                    "search_text": chunk.search_text,
                    "heading_text": " ".join(
                        value
                        for value in (chunk.heading_breadcrumb, chunk.heading)
                        if value
                    ),
                    "metadata": json.dumps(chunk.metadata, ensure_ascii=False),
                },
            )

        await connection.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET status = 'READY', progress = 100, error = NULL, updated_at = now()
                WHERE document_id = :document_id AND status = 'PROCESSING'
                """
            ),
            {"document_id": document_id},
        )
        await connection.execute(
            text("UPDATE documents SET status = 'READY' WHERE id = :document_id"),
            {"document_id": document_id},
        )


async def _mark_failed(job_id: uuid.UUID, document_id: uuid.UUID | None, error: Exception) -> None:
    message = str(error)[:4000] or error.__class__.__name__
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                UPDATE ingestion_jobs
                SET status = 'FAILED', error = :error, updated_at = now()
                WHERE id = :job_id
                """
            ),
            {"job_id": job_id, "error": message},
        )
        if document_id is not None:
            await connection.execute(
                text("UPDATE documents SET status = 'FAILED' WHERE id = :document_id"),
                {"document_id": document_id},
            )


async def process_job(job_id: uuid.UUID) -> None:
    job = await _claim_job(job_id)
    if job is None:
        logger.info("Skipping unavailable or already claimed job %s", job_id)
        return

    document_id = job["document_id"]
    try:
        data = await asyncio.to_thread(storage.download, job["object_key"])
        chunks = await asyncio.to_thread(parse_document, job["filename"], data)
        if not chunks:
            raise ValueError("Document produced no chunks")

        vectors = await embeddings.embed([chunk.search_text for chunk in chunks])
        await _store_chunks(document_id, chunks, vectors)
        logger.info("Document %s indexed into %d chunks", document_id, len(chunks))
    except Exception as exc:
        logger.exception("Ingestion job %s failed", job_id)
        await _mark_failed(job_id, document_id, exc)


async def main() -> None:
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    logger.info("RAG worker started; queue=%s", settings.ingestion_queue)

    try:
        while True:
            try:
                item = await redis.blpop(settings.ingestion_queue, timeout=5)
                if item is None:
                    continue

                _, raw_job_id = item
                try:
                    job_id = uuid.UUID(raw_job_id)
                except ValueError:
                    logger.error("Ignoring malformed job id: %s", raw_job_id)
                    continue

                await process_job(job_id)
            except RedisError:
                logger.exception("Redis is unavailable; retrying")
                await asyncio.sleep(2)
    finally:
        await redis.aclose()
        await embeddings.close()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
