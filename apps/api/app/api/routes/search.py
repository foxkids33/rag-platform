from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Workspace
from app.db.session import get_db
from app.services.embeddings import EmbeddingError, embeddings

router = APIRouter(prefix="/workspaces/{workspace_id}/search", tags=["search"])


class SearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    limit: int = Field(default=8, ge=1, le=20)


class SearchResult(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    chunk_index: int
    text: str
    parent_text: str | None
    heading: str | None
    heading_breadcrumb: str | None
    page_start: int | None
    page_end: int | None
    score: float


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResult]


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{value:.9g}" for value in vector) + "]"


@router.post("", response_model=SearchResponse)
async def semantic_search(
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

    try:
        query_vector = await embeddings.embed_query(query)
    except EmbeddingError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    result = await db.execute(
        text(
            """
            SELECT
                c.id AS chunk_id,
                c.document_id,
                d.filename,
                c.chunk_index,
                c.text,
                c.parent_text,
                c.heading,
                c.heading_breadcrumb,
                c.page_start,
                c.page_end,
                1 - (c.embedding <=> CAST(:query_vector AS vector)) AS score
            FROM document_chunks AS c
            JOIN documents AS d ON d.id = c.document_id
            WHERE d.workspace_id = :workspace_id
              AND d.status = 'READY'
              AND c.embedding IS NOT NULL
            ORDER BY c.embedding <=> CAST(:query_vector AS vector)
            LIMIT :limit
            """
        ),
        {
            "workspace_id": workspace_id,
            "query_vector": _vector_literal(query_vector),
            "limit": payload.limit,
        },
    )

    rows = [SearchResult(**dict(row)) for row in result.mappings().all()]
    return SearchResponse(query=query, results=rows)
