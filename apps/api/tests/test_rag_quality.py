from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.services.context_builder import build_context
from app.services.rag_quality import audit_citations, source_quality_score


@dataclass
class Candidate:
    text: str
    document_id: uuid.UUID
    retrieval_rank: int
    rerank_score: float | None
    dense_score: float | None = 0.5
    lexical_score: float | None = 0.01
    rrf_score: float = 0.03
    filename: str = "document.pdf"
    chunk_index: int = 0
    parent_text: str | None = None
    heading: str | None = "Раздел"
    heading_breadcrumb: str | None = "1. Раздел"
    page_start: int | None = 1
    page_end: int | None = 1
    rerank_fusion_score: float | None = 0.02
    chunk_id: uuid.UUID = field(default_factory=uuid.uuid4)


def test_context_abstains_when_best_evidence_is_too_weak() -> None:
    candidate = Candidate(
        text="Случайный фрагмент, не относящийся к вопросу.",
        document_id=uuid.uuid4(),
        retrieval_rank=1,
        rerank_score=0.04,
    )

    context = build_context(
        "Как устроен продукт?",
        [candidate],
        max_context_chars=2000,
        max_source_chars=1000,
        max_sources=5,
        strong_evidence_score=0.5,
        limited_evidence_score=0.15,
    )

    assert context.evidence_status == "insufficient"
    assert context.sources == []
    assert context.rejected_low_score == 1


def test_context_removes_duplicates_and_limits_one_document() -> None:
    document_id = uuid.uuid4()
    candidates = [
        Candidate(
            text="Продукт хранит и обрабатывает большие объёмы данных в единой системе.",
            document_id=document_id,
            retrieval_rank=1,
            rerank_score=0.95,
            chunk_index=1,
            chunk_id=uuid.uuid4(),
        ),
        Candidate(
            text="Продукт хранит и обрабатывает большие объёмы данных в единой системе.",
            document_id=document_id,
            retrieval_rank=2,
            rerank_score=0.90,
            chunk_index=2,
            chunk_id=uuid.uuid4(),
        ),
        Candidate(
            text="Архитектура поддерживает горизонтальное масштабирование вычислительных узлов.",
            document_id=document_id,
            retrieval_rank=3,
            rerank_score=0.80,
            chunk_index=3,
            chunk_id=uuid.uuid4(),
        ),
        Candidate(
            text="Доступ к данным защищён централизованными политиками безопасности.",
            document_id=document_id,
            retrieval_rank=4,
            rerank_score=0.70,
            chunk_index=4,
            chunk_id=uuid.uuid4(),
        ),
    ]

    context = build_context(
        "Что умеет продукт?",
        candidates,
        max_context_chars=4000,
        max_source_chars=1000,
        max_sources=5,
        max_sources_per_document=2,
        source_similarity_threshold=0.75,
    )

    assert len(context.sources) == 2
    assert context.rejected_duplicate == 1
    assert context.rejected_document_cap == 1


def test_context_promotes_exact_query_signals_from_below_source_limit() -> None:
    distractors = [
        Candidate(
            text=(
                f"Общий архитектурный фрагмент номер {index} описывает вычислительные "
                "узлы, эксплуатацию и масштабирование корпоративной платформы."
            ),
            document_id=uuid.uuid4(),
            retrieval_rank=index,
            rerank_score=None,
            dense_score=0.70,
            chunk_index=index,
        )
        for index in range(1, 6)
    ]
    relevant = Candidate(
        text=(
            "МБД.Г предназначена для аналитических запросов и использует "
            "СУБД Arenadata DB для обработки структурированных данных."
        ),
        document_id=uuid.uuid4(),
        retrieval_rank=6,
        rerank_score=None,
        dense_score=0.65,
        chunk_index=6,
        filename="relevant.txt",
    )

    context = build_context(
        "Для какой СУБД предназначена МБД.Г?",
        [*distractors, relevant],
        max_context_chars=5000,
        max_source_chars=1000,
        max_sources=5,
        query_selection_weight=0.30,
    )

    assert context.sources[0].filename == "relevant.txt"
    assert context.sources[0].retrieval_rank == 6
    assert context.sources[0].query_relevance_score == 1.0
    assert context.sources[0].selection_score > context.sources[1].selection_score


def test_context_query_selector_can_be_disabled() -> None:
    candidates = [
        Candidate(
            text=f"Нерелевантный фрагмент {index} с общим описанием инфраструктуры.",
            document_id=uuid.uuid4(),
            retrieval_rank=index,
            rerank_score=None,
            dense_score=0.70,
            chunk_index=index,
            filename=f"rank-{index}.txt",
        )
        for index in range(1, 6)
    ]
    candidates.append(
        Candidate(
            text="Версия 2.1 выпущена 01.09.2025 и описывает целевой продукт.",
            document_id=uuid.uuid4(),
            retrieval_rank=6,
            rerank_score=None,
            dense_score=0.65,
            chunk_index=6,
            filename="relevant.txt",
        )
    )

    context = build_context(
        "Какой продукт имеет версию 2.1 от 01.09.2025?",
        candidates,
        max_context_chars=5000,
        max_source_chars=1000,
        max_sources=5,
        source_similarity_threshold=1.1,
        query_selection_weight=0.0,
    )

    assert [source.filename for source in context.sources] == [
        f"rank-{index}.txt" for index in range(1, 6)
    ]


def test_context_selector_does_not_override_strong_reranker_gap() -> None:
    reranked = [
        Candidate(
            text="Семантически релевантное описание целевого решения без повторения вопроса.",
            document_id=uuid.uuid4(),
            retrieval_rank=1,
            rerank_score=0.95,
            filename="reranker-winner.txt",
        ),
        Candidate(
            text="Версия 2.1 от 01.09.2025 упомянута буквально.",
            document_id=uuid.uuid4(),
            retrieval_rank=2,
            rerank_score=0.30,
            filename="literal-match.txt",
        ),
    ]

    context = build_context(
        "Какая версия 2.1 опубликована 01.09.2025?",
        reranked,
        max_context_chars=3000,
        max_source_chars=1000,
        max_sources=2,
        query_selection_weight=0.30,
    )

    assert context.sources[0].filename == "reranker-winner.txt"
    assert context.sources[0].selection_score > context.sources[1].selection_score


def test_citation_audit_rejects_unknown_indices() -> None:
    audit = audit_citations("Факт подтверждён [1], но ссылка [9] не существует.", [1, 2])

    assert audit.valid is False
    assert audit.cited_source_indices == [1]
    assert audit.invalid_citations == [9]


def test_source_quality_prefers_reranker_score() -> None:
    candidate = Candidate(
        text="Релевантный фрагмент.",
        document_id=uuid.uuid4(),
        retrieval_rank=1,
        rerank_score=0.82,
        dense_score=0.20,
    )

    assert source_quality_score(candidate) == 0.82
