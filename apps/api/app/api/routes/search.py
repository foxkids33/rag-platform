from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.models import Workspace
from app.db.session import get_db
from app.services.embeddings import EmbeddingError, embeddings
from app.services.reranker import RerankError, reranker

router = APIRouter(prefix="/workspaces/{workspace_id}/search", tags=["search"])

RRF_K = 60
MIN_CANDIDATES = 40
MAX_CANDIDATES = 200
PARENT_PREVIEW_CHARS = 4000


class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    limit: int = Field(default=8, ge=1, le=20)
    mode: Literal["hybrid", "semantic", "lexical"] = "hybrid"
    rerank: bool = True


class SearchResult(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    text: str
    parent_text: str | None
    parent_text_truncated: bool
    heading: str | None
    heading_breadcrumb: str | None
    page_start: int | None
    page_end: int | None
    score: float | None
    dense_score: float | None
    lexical_score: float | None
    rrf_score: float
    dense_rank: int | None
    lexical_rank: int | None
    retrieval_rank: int
    rerank_score: float | None


class SearchResponse(BaseModel):
    query: str
    mode: str
    candidate_limit: int
    rerank_requested: bool
    rerank_applied: bool
    rerank_model: str | None
    rerank_error: str | None
    results: list[SearchResult]


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.9g}" for value in vector) + "]"


def _candidate_limit(limit: int) -> int:
    return min(MAX_CANDIDATES, max(MIN_CANDIDATES, limit * 8))


def _rerank_candidate_limit(limit: int, retrieval_limit: int) -> int:
    requested = max(limit, settings.rerank_candidate_limit)
    return min(retrieval_limit, requested)


def _build_rerank_document(result: SearchResult) -> str:
    parts: list[str] = []
    if result.heading_breadcrumb:
        parts.append(result.heading_breadcrumb)
    elif result.heading:
        parts.append(result.heading)
    parts.append(result.text)
    return "\n\n".join(parts)[: settings.rerank_document_chars]


@router.post("", response_model=SearchResponse)
async def search(
    workspace_id: uuid.UUID,
    payload: SearchRequest,
    db: AsyncSession = Depends(get_db),
) -> SearchResponse:
    workspace = await db.get(Workspace, workspace_id)
    if workspace is None:
        raise HTTPException(status_code=404, detail="Workspace not found")

    query = payload.query.strip()
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="Query is too short")

    use_dense = payload.mode in {"hybrid", "semantic"}
    use_lexical = payload.mode in {"hybrid", "lexical"}

    if use_dense:
        try:
            query_vector = await embeddings.embed_query(query)
        except EmbeddingError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
    else:
        query_vector = [0.0] * settings.embedding_dim

    candidate_limit = _candidate_limit(payload.limit)
    rerank_requested = payload.rerank and settings.rerank_enabled
    result_limit = (
        _rerank_candidate_limit(payload.limit, candidate_limit)
        if rerank_requested
        else payload.limit
    )

    result = await db.execute(
        text(
            """
            WITH query_input AS (
                SELECT
                    CAST(:query_vector AS vector) AS query_vector,
                    (
                        websearch_to_tsquery('russian', :query)
                        || websearch_to_tsquery('simple', :query)
                    ) AS ts_query
            ),
            eligible AS NOT MATERIALIZED (
                SELECT
                    c.id,
                    c.document_id,
                    c.parent_chunk_id,
                    c.chunk_index,
                    c.text,
                    c.parent_text,
                    c.heading,
                    c.heading_breadcrumb,
                    c.page_start,
                    c.page_end,
                    c.embedding,
                    c.search_vector,
                    d.filename
                FROM document_chunks AS c
                JOIN documents AS d ON d.id = c.document_id
                WHERE d.workspace_id = :workspace_id
                  AND d.status = 'READY'
            ),
            dense AS (
                SELECT
                    e.id,
                    row_number() OVER (
                        ORDER BY e.embedding <=> q.query_vector
                    ) AS dense_rank,
                    1 - (e.embedding <=> q.query_vector) AS dense_score
                FROM eligible AS e
                CROSS JOIN query_input AS q
                WHERE :use_dense
                  AND e.embedding IS NOT NULL
                ORDER BY e.embedding <=> q.query_vector
                LIMIT :candidate_limit
            ),
            lexical AS (
                SELECT
                    e.id,
                    row_number() OVER (
                        ORDER BY ts_rank_cd(e.search_vector, q.ts_query, 32) DESC
                    ) AS lexical_rank,
                    ts_rank_cd(e.search_vector, q.ts_query, 32) AS lexical_score
                FROM eligible AS e
                CROSS JOIN query_input AS q
                WHERE :use_lexical
                  AND e.search_vector IS NOT NULL
                  AND e.search_vector @@ q.ts_query
                ORDER BY ts_rank_cd(e.search_vector, q.ts_query, 32) DESC
                LIMIT :candidate_limit
            ),
            fused AS (
                SELECT
                    COALESCE(d.id, l.id) AS id,
                    d.dense_rank,
                    l.lexical_rank,
                    d.dense_score,
                    l.lexical_score,
                    (
                        CASE
                            WHEN d.dense_rank IS NULL THEN 0.0
                            ELSE 1.0 / (:rrf_k + d.dense_rank)
                        END
                        +
                        CASE
                            WHEN l.lexical_rank IS NULL THEN 0.0
                            ELSE 1.0 / (:rrf_k + l.lexical_rank)
                        END
                    )::double precision AS rrf_score
                FROM dense AS d
                FULL OUTER JOIN lexical AS l ON l.id = d.id
            ),
            deduplicated AS (
                SELECT
                    e.id AS chunk_id,
                    e.document_id,
                    e.filename,
                    e.chunk_index,
                    e.text,
                    LEFT(e.parent_text, :parent_preview_chars) AS parent_text,
                    COALESCE(length(e.parent_text) > :parent_preview_chars, false)
                        AS parent_text_truncated,
                    e.heading,
                    e.heading_breadcrumb,
                    e.page_start,
                    e.page_end,
                    f.dense_score,
                    f.lexical_score,
                    f.rrf_score,
                    f.dense_rank,
                    f.lexical_rank,
                    row_number() OVER (
                        PARTITION BY COALESCE(e.parent_chunk_id, e.id)
                        ORDER BY
                            f.rrf_score DESC,
                            f.dense_score DESC NULLS LAST,
                            f.lexical_score DESC NULLS LAST,
                            e.chunk_index
                    ) AS parent_position
                FROM fused AS f
                JOIN eligible AS e ON e.id = f.id
            )
            SELECT
                chunk_id,
                document_id,
                filename,
                chunk_index,
                text,
                parent_text,
                parent_text_truncated,
                heading,
                heading_breadcrumb,
                page_start,
                page_end,
                dense_score AS score,
                dense_score,
                lexical_score,
                rrf_score,
                dense_rank,
                lexical_rank
            FROM deduplicated
            WHERE parent_position = 1
            ORDER BY
                rrf_score DESC,
                dense_score DESC NULLS LAST,
                lexical_score DESC NULLS LAST
            LIMIT :result_limit
            """
        ),
        {
            "workspace_id": workspace_id,
            "query": query,
            "query_vector": _vector_literal(query_vector),
            "use_dense": use_dense,
            "use_lexical": use_lexical,
            "candidate_limit": candidate_limit,
            "rrf_k": RRF_K,
            "parent_preview_chars": PARENT_PREVIEW_CHARS,
            "result_limit": result_limit,
        },
    )

    rows: list[SearchResult] = []
    for retrieval_rank, row in enumerate(result.mappings().all(), start=1):
        data = dict(row)
        data["retrieval_rank"] = retrieval_rank
        data["rerank_score"] = None
        rows.append(SearchResult(**data))

    rerank_applied = False
    rerank_model: str | None = None
    rerank_error: str | None = None

    if rerank_requested and rows:
        documents = [_build_rerank_document(row) for row in rows]
        try:
            rerank_output = await reranker.rerank(query, documents)
        except RerankError as exc:
            rerank_error = str(exc)
        else:
            rerank_model = rerank_output.model
            rows = [
                row.model_copy(update={"rerank_score": score})
                for row, score in zip(rows, rerank_output.scores, strict=True)
            ]
            rows.sort(
                key=lambda row: (
                    -(row.rerank_score if row.rerank_score is not None else float("-inf")),
                    row.retrieval_rank,
                )
            )
            rerank_applied = True

    return SearchResponse(
        query=query,
        mode=payload.mode,
        candidate_limit=candidate_limit,
        rerank_requested=rerank_requested,
        rerank_applied=rerank_applied,
        rerank_model=rerank_model,
        rerank_error=rerank_error,
        results=rows[: payload.limit],
    )
