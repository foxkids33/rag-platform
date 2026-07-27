import uuid

from app.api.routes.search import (
    SearchResult,
    _boilerplate_penalty,
    _build_rerank_document,
    _candidate_limit,
    _clean_rerank_text,
    _focused_excerpt,
    _rerank_candidate_limit,
    _rerank_fusion_score,
    _vector_literal,
)


def test_candidate_limit_has_retrieval_headroom() -> None:
    assert _candidate_limit(1) == 40
    assert _candidate_limit(8) == 64
    assert _candidate_limit(20) == 160


def test_rerank_candidate_limit_is_bounded_by_retrieval_pool() -> None:
    assert _rerank_candidate_limit(5, 40) == 20
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

    assert _build_rerank_document(result, "What is it?") == (
        "Chapter > Section\n\nDirect answer"
    )


def test_clean_rerank_text_removes_pdf_boilerplate() -> None:
    source = """
Машина больших данных Скала^р МБД.Х. Технический обзор

2 из 50

ОГЛАВЛЕНИЕ
Введение .......................................................... 10
info@skala-r.ru

МБД.Х — это программно-аппаратный комплекс для хранения данных.
"""

    assert _clean_rerank_text(source) == (
        "МБД.Х — это программно-аппаратный комплекс для хранения данных."
    )


def test_focused_excerpt_prefers_definition_over_unrelated_text() -> None:
    source = (
        "Техническая поддержка предоставляется круглосуточно.\n\n"
        + "Шумный раздел. " * 200
        + "\n\nМБД.Х — это программно-аппаратный комплекс, предназначенный "
        "для создания централизованного хранилища данных.\n\n"
        + "Дополнительные сведения. " * 200
    )

    excerpt = _focused_excerpt(
        source,
        "Что такое машина больших данных Скала МБД.Х?",
        500,
    )

    assert "МБД.Х — это программно-аппаратный комплекс" in excerpt
    assert len(excerpt) <= 500


def test_boilerplate_penalty_marks_title_and_toc_chunk() -> None:
    result = SearchResult(
        chunk_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        filename="doc.txt",
        chunk_index=0,
        text=(
            "ОГЛАВЛЕНИЕ\n"
            "Введение ........................................ 10\n"
            "Архитектура ..................................... 20\n"
            "Поддержка ........................................ 40"
        ),
        parent_text=None,
        parent_text_truncated=False,
        heading=None,
        heading_breadcrumb=None,
        page_start=None,
        page_end=None,
        score=0.5,
        dense_score=0.5,
        lexical_score=None,
        rrf_score=0.01,
        dense_rank=10,
        lexical_rank=None,
        retrieval_rank=9,
    )

    assert _boilerplate_penalty(result) == 0.55


def test_rank_fusion_prefers_retrieval_backed_definition_over_toc() -> None:
    title_score = _rerank_fusion_score(
        rerank_rank=1,
        retrieval_rank=9,
        penalty=0.55,
    )
    definition_score = _rerank_fusion_score(
        rerank_rank=2,
        retrieval_rank=1,
        penalty=0.0,
    )

    assert definition_score > title_score
