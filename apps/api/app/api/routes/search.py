from __future__ import annotations

import re
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import workspace_for_principal
from app.core.config import settings
from app.core.security import Principal, get_current_principal
from app.db.models import KnowledgeBase, KnowledgeBaseVersion
from app.db.session import get_db
from app.services.embeddings import EmbeddingError, embeddings
from app.services.reranker import RerankError, reranker
from app.services.workspace_sources import (
    WorkspaceSourceMode,
    includes_knowledge_base,
    includes_workspace_documents,
)

router = APIRouter(prefix="/workspaces/{workspace_id}/search", tags=["search"])

RRF_K = 60
MIN_CANDIDATES = 40
MAX_CANDIDATES = 200
PARENT_PREVIEW_CHARS = 4000

PAGE_MARKER_RE = re.compile(r"^\s*\d{1,4}\s+из\s+\d{1,4}\s*$", re.IGNORECASE)
STANDALONE_PAGE_RE = re.compile(r"^\s*\d{1,4}\s*$")
DOT_LEADER_RE = re.compile(r"\.{4,}")
DOCUMENT_TITLE_RE = re.compile(
    r"машина\s+больших\s+данных.*техническ(?:ий|ого)\s+обзор",
    re.IGNORECASE,
)
URL_OR_EMAIL_RE = re.compile(r"(?:https?://|www\.|\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,})", re.IGNORECASE)
WORD_RE = re.compile(r"[0-9A-Za-zА-Яа-яЁё]+(?:[.\-][0-9A-Za-zА-Яа-яЁё]+)*")
QUERY_STOP_WORDS = {
    "что",
    "такое",
    "какой",
    "какая",
    "какие",
    "как",
    "для",
    "чего",
    "это",
    "есть",
    "или",
    "его",
    "ее",
    "её",
    "про",
    "при",
    "под",
    "над",
    "где",
    "когда",
    "зачем",
}


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
    rerank_score: float | None = None
    rerank_rank: int | None = None
    rerank_fusion_score: float | None = None
    rerank_penalty: float = 0.0
    source_scope: Literal["workspace", "knowledge_base"] = "workspace"
    knowledge_base_name: str | None = None
    knowledge_base_version: int | None = None


class SearchResponse(BaseModel):
    query: str
    mode: str
    workspace_source_mode: WorkspaceSourceMode
    knowledge_base_id: uuid.UUID | None
    knowledge_base_version_id: uuid.UUID | None
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


def _clean_rerank_text(value: str) -> str:
    """Remove PDF-to-text boilerplate that biases cross-encoder scores."""
    cleaned_lines: list[str] = []
    previous_line: str | None = None

    for raw_line in value.replace("\x0c", "\n").splitlines():
        line = " ".join(raw_line.split()).strip()
        if not line:
            if cleaned_lines and cleaned_lines[-1] != "":
                cleaned_lines.append("")
            continue
        if PAGE_MARKER_RE.fullmatch(line) or STANDALONE_PAGE_RE.fullmatch(line):
            continue
        if DOT_LEADER_RE.search(line):
            continue
        if DOCUMENT_TITLE_RE.search(line):
            continue
        if URL_OR_EMAIL_RE.search(line):
            continue
        if line.casefold() in {"оглавление", "содержание"}:
            continue
        if line == previous_line:
            continue

        cleaned_lines.append(line)
        previous_line = line

    while cleaned_lines and cleaned_lines[-1] == "":
        cleaned_lines.pop()
    return re.sub(r"\n{3,}", "\n\n", "\n".join(cleaned_lines)).strip()


def _query_terms(query: str) -> set[str]:
    return {
        token.casefold()
        for token in WORD_RE.findall(query)
        if len(token) >= 3 and token.casefold() not in QUERY_STOP_WORDS
    }


def _focused_excerpt(value: str, query: str, max_chars: int) -> str:
    cleaned = _clean_rerank_text(value)
    if len(cleaned) <= max_chars:
        return cleaned

    terms = _query_terms(query)
    paragraphs = [part.strip() for part in cleaned.split("\n\n") if part.strip()]
    if not paragraphs:
        return cleaned[:max_chars].rstrip()

    def paragraph_score(paragraph: str) -> tuple[float, int]:
        paragraph_terms = {token.casefold() for token in WORD_RE.findall(paragraph)}
        overlap = len(terms & paragraph_terms)
        score = float(overlap)
        lowered = paragraph.casefold()
        if query.casefold().startswith("что такое") and any(
            marker in lowered for marker in (" — это ", " это ", "предназначен", "предназначена")
        ):
            score += 1.5
        return score, -len(paragraph)

    best_index = max(range(len(paragraphs)), key=lambda index: paragraph_score(paragraphs[index]))
    selected_indices = {best_index}
    left = best_index - 1
    right = best_index + 1

    while left >= 0 or right < len(paragraphs):
        candidate_indices: list[int] = []
        if right < len(paragraphs):
            candidate_indices.append(right)
        if left >= 0:
            candidate_indices.append(left)

        added = False
        for index in candidate_indices:
            trial_indices = sorted(selected_indices | {index})
            candidate = "\n\n".join(paragraphs[item] for item in trial_indices)
            if len(candidate) <= max_chars:
                selected_indices.add(index)
                added = True
                if index == left:
                    left -= 1
                else:
                    right += 1
                break
        if not added:
            break

    excerpt = "\n\n".join(paragraphs[index] for index in sorted(selected_indices))
    return excerpt[:max_chars].rstrip()


def _build_rerank_document(result: SearchResult, query: str) -> str:
    parts: list[str] = []
    heading = result.heading_breadcrumb or result.heading
    if heading:
        cleaned_heading = _clean_rerank_text(heading)
        if cleaned_heading:
            parts.append(cleaned_heading)

    excerpt = _focused_excerpt(result.text, query, settings.rerank_document_chars)
    if excerpt:
        parts.append(excerpt)
    return "\n\n".join(parts)[: settings.rerank_document_chars]


def _boilerplate_penalty(result: SearchResult) -> float:
    text_value = result.text.casefold()
    penalty = 0.0
    if result.chunk_index == 0:
        penalty += 0.25
    if "оглавление" in text_value or "содержание" in text_value:
        penalty += 0.25
    if len(DOT_LEADER_RE.findall(result.text)) >= 3:
        penalty += 0.15
    return min(0.55, penalty)


def _rerank_fusion_score(
    rerank_rank: int,
    retrieval_rank: int,
    penalty: float,
) -> float:
    rerank_component = settings.rerank_rank_weight / (RRF_K + rerank_rank)
    retrieval_component = settings.rerank_retrieval_weight / (RRF_K + retrieval_rank)
    return (rerank_component + retrieval_component) * (1.0 - penalty)


@router.post("", response_model=SearchResponse)
async def search(
    workspace_id: uuid.UUID,
    payload: SearchRequest,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_current_principal),
) -> SearchResponse:
    workspace = await workspace_for_principal(db, workspace_id, principal)

    source_mode = WorkspaceSourceMode(workspace.source_mode)
    include_workspace = includes_workspace_documents(source_mode)
    include_knowledge_base = includes_knowledge_base(source_mode)
    knowledge_base: KnowledgeBase | None = None
    active_version: KnowledgeBaseVersion | None = None
    if workspace.base_knowledge_base_id is not None:
        knowledge_base = await db.get(KnowledgeBase, workspace.base_knowledge_base_id)
        if knowledge_base is not None and knowledge_base.tenant_id != principal.tenant_id:
            knowledge_base = None
        if knowledge_base is not None and knowledge_base.active_version_id is not None:
            active_version = await db.get(KnowledgeBaseVersion, knowledge_base.active_version_id)

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
                    d.filename,
                    CASE
                        WHEN d.workspace_id IS NOT NULL THEN 'workspace'
                        ELSE 'knowledge_base'
                    END AS source_scope,
                    kb.name AS knowledge_base_name,
                    kbv.version AS knowledge_base_version
                FROM document_chunks AS c
                JOIN documents AS d ON d.id = c.document_id
                LEFT JOIN knowledge_base_versions AS kbv
                    ON kbv.id = d.knowledge_base_version_id
                LEFT JOIN knowledge_bases AS kb
                    ON kb.id = kbv.knowledge_base_id
                WHERE d.status = 'READY'
                  AND d.search_enabled IS TRUE
                  AND (
                      (:include_workspace AND d.workspace_id = :workspace_id)
                      OR
                      (
                          :include_knowledge_base
                          AND d.knowledge_base_version_id = :knowledge_base_version_id
                          AND kb.tenant_id = :tenant_id
                      )
                  )
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
                    e.source_scope,
                    e.knowledge_base_name,
                    e.knowledge_base_version,
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
                source_scope,
                knowledge_base_name,
                knowledge_base_version,
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
            "tenant_id": principal.tenant_id,
            "include_workspace": include_workspace,
            "include_knowledge_base": include_knowledge_base and active_version is not None,
            "knowledge_base_version_id": (
                active_version.id if active_version is not None else uuid.UUID(int=0)
            ),
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
        rows.append(SearchResult(**data))

    rerank_applied = False
    rerank_model: str | None = None
    rerank_error: str | None = None

    if rerank_requested and rows:
        documents = [_build_rerank_document(row, query) for row in rows]
        try:
            rerank_output = await reranker.rerank(query, documents)
        except RerankError as exc:
            rerank_error = str(exc)
        else:
            rerank_model = rerank_output.model
            score_pairs = sorted(
                zip(rows, rerank_output.scores, strict=True),
                key=lambda pair: (-pair[1], pair[0].retrieval_rank),
            )
            rerank_ranks = {row.chunk_id: rank for rank, (row, _) in enumerate(score_pairs, 1)}

            reranked_rows: list[SearchResult] = []
            for row, score in zip(rows, rerank_output.scores, strict=True):
                rerank_rank = rerank_ranks[row.chunk_id]
                penalty = _boilerplate_penalty(row)
                fusion_score = _rerank_fusion_score(
                    rerank_rank=rerank_rank,
                    retrieval_rank=row.retrieval_rank,
                    penalty=penalty,
                )
                reranked_rows.append(
                    row.model_copy(
                        update={
                            "rerank_score": score,
                            "rerank_rank": rerank_rank,
                            "rerank_fusion_score": fusion_score,
                            "rerank_penalty": penalty,
                        }
                    )
                )

            reranked_rows.sort(
                key=lambda row: (
                    -(row.rerank_fusion_score or 0.0),
                    row.rerank_rank or MAX_CANDIDATES,
                    row.retrieval_rank,
                )
            )
            rows = reranked_rows
            rerank_applied = True

    return SearchResponse(
        query=query,
        mode=payload.mode,
        workspace_source_mode=source_mode,
        knowledge_base_id=knowledge_base.id if knowledge_base else None,
        knowledge_base_version_id=active_version.id if active_version else None,
        candidate_limit=candidate_limit,
        rerank_requested=rerank_requested,
        rerank_applied=rerank_applied,
        rerank_model=rerank_model,
        rerank_error=rerank_error,
        results=rows[: payload.limit],
    )
