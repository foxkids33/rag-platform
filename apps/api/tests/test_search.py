import uuid

from app.api.routes.search import (
    SearchResult,
    _build_rerank_document,
    _candidate_limit,
    _rerank_candidate_limit,
    _vector_literal,
)


def test_candidate_limit_has_retrieval_headroom() -> None:
    assert _candidate_limit(1) == 40
    assert _candidate_limit(8) == 64
    assert _candidate_limit(20) == 160


def test_rerank_candidate_limit_is_bounded_by_retrieval_pool() -> None:
    assert _rerank_candidate_limit(5, 40) == 30
    assert _rerank_candidate_limit(20, 20) == 20


def test_vector_literal_is_pgvector_compatible() -> None:
    assert _vector_literal([0.5, -1.25, 0.0]) == "[0.5,-1.25,0]"


def test_rerank_document_prefers_breadcrumb_and_child_text() -> None:
    result = SearchResult(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="doc.pdf",
        chunk_index=1,
        text="Direct answer",
        parent_text="Very large parent context",
        parent_text_truncated=False,
        heading="Section",
        heading_breadcrumb="Chapter > Section",
        page_start=2,
        page_end=2,
        score=0.7,
        dense_score=0.7,
        lexical_score=0.2,
        rrf_score=0.03,
        dense_rank=1,
        lexical_rank=2,
        retrieval_rank=1,
        rerank_score=None,
    )

    assert _build_rerank_document(result) == "Chapter > Section\n\nDirect answer"
