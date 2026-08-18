from __future__ import annotations

import math

import pytest

from app.evaluation.metrics import (
    aggregate_answer_metrics,
    aggregate_retrieval_metrics,
    evaluate_answer_case,
    evaluate_retrieval_case,
    fact_coverage,
    ndcg_at_k,
    normalize_for_match,
    recall_at_k,
    reciprocal_rank,
)
from app.evaluation.models import (
    AnswerObservation,
    EvaluationCase,
    RetrievalObservation,
)


def test_recall_at_k_finds_relevant_document() -> None:
    expected = ("document-a",)
    retrieved = ("document-b", "document-a", "document-c")

    assert recall_at_k(expected, retrieved, k=1) == 0.0
    assert recall_at_k(expected, retrieved, k=5) == 1.0


def test_recall_at_k_supports_multiple_expected_documents() -> None:
    expected = ("document-a", "document-b")
    retrieved = ("document-a", "document-c")

    assert recall_at_k(expected, retrieved, k=5) == 0.5


def test_reciprocal_rank_uses_first_relevant_document() -> None:
    expected = ("document-a",)
    retrieved = ("document-c", "document-b", "document-a")

    assert reciprocal_rank(expected, retrieved) == pytest.approx(1 / 3)


def test_ndcg_rewards_earlier_and_complete_retrieval() -> None:
    expected = ("document-a", "document-b")

    assert ndcg_at_k(expected, ("document-a", "document-b"), k=10) == 1.0
    assert ndcg_at_k(expected, ("document-c", "document-a"), k=10) < 1.0


def test_fact_matching_normalizes_typography_and_pdf_hyphenation() -> None:
    text = "Виртуаль- ных машин: 20–28 узлов, REST API."

    assert normalize_for_match("виртуальных") in normalize_for_match(text)
    assert fact_coverage(("20-28 узлов", "REST API"), text) == 1.0


def test_answer_metrics_check_facts_values_forbidden_text_and_citations() -> None:
    case = EvaluationCase(
        id="exact-001",
        question="Какая версия?",
        question_type="exact_value",
        expected_answer="2.1",
        required_facts=("2.1", "01.09.2025"),
        forbidden_facts=("3.1",),
    )
    result = evaluate_answer_case(
        case,
        AnswerObservation(
            case_id=case.id,
            text="Версия 2.1 от 01.09.2025 [1].",
            actual_abstention=False,
            citation_valid=True,
            cited_source_text="Технический обзор версия 2.1 от 01.09.2025",
            cited_filenames=("review.txt",),
            context_source_text="Технический обзор версия 2.1 от 01.09.2025",
        ),
    )

    assert result.required_fact_coverage == 1.0
    assert result.exact_value_correct is True
    assert result.forbidden_fact_violation is False
    assert result.context_fact_coverage == 1.0
    assert result.context_source_coverage is None
    assert result.gold_context_fact_coverage is None
    assert result.citation_fact_coverage == 1.0
    assert result.citation_source_coverage is None
    assert result.citation_valid is True
    aggregate = aggregate_answer_metrics([result])
    assert aggregate.exact_value_accuracy == 1.0
    assert aggregate.forbidden_fact_violation_rate == 0.0
    assert aggregate.context_fact_evaluated_cases == 1
    assert aggregate.context_fact_coverage == 1.0


def test_context_metrics_separate_document_presence_from_gold_chunk_facts() -> None:
    case = EvaluationCase(
        id="context-001",
        question="Какая версия и дата?",
        expected_filenames=("review.txt",),
        required_facts=("2.1", "01.09.2025"),
    )

    result = evaluate_answer_case(
        case,
        AnswerObservation(
            case_id=case.id,
            text="Недостаточно информации.",
            actual_abstention=True,
            citation_valid=True,
            cited_source_text="",
            context_source_text=(
                "Фрагмент review.txt содержит версию 2.1. "
                "Другой документ содержит дату 01.09.2025."
            ),
            context_filenames=("review.txt", "other.txt"),
            gold_context_source_text="Фрагмент review.txt содержит версию 2.1.",
        ),
    )

    assert result.required_fact_coverage == 0.0
    assert result.context_fact_coverage == 1.0
    assert result.context_source_coverage == 1.0
    assert result.gold_context_fact_coverage == 0.5
    assert result.citation_fact_coverage is None
    assert result.citation_source_coverage is None


def test_exact_value_uses_atomic_required_facts_instead_of_full_answer_phrase() -> None:
    case = EvaluationCase(
        id="exact-atomic",
        question="Какие скорости?",
        expected_filenames=("review.txt",),
        question_type="exact_value",
        expected_answer="25/100 Гбит/с",
        required_facts=("25 Гбит/с", "100 Гбит/с"),
    )
    result = evaluate_answer_case(
        case,
        AnswerObservation(
            case_id=case.id,
            text="Внутренняя сеть: 25 Гбит/с; внешняя сеть: 100 Гбит/с [1].",
            actual_abstention=False,
            citation_valid=True,
            cited_source_text="25 Гбит/с и 100 Гбит/с",
            cited_filenames=("review.txt",),
        ),
    )

    assert result.required_fact_coverage == 1.0
    assert result.exact_value_correct is True
    assert result.citation_source_coverage == 1.0


def test_abstained_answer_gets_zero_fact_and_exact_value_credit() -> None:
    case = EvaluationCase(
        id="exact-refusal",
        question="Какая версия?",
        question_type="exact_value",
        expected_answer="2.1",
        required_facts=("2.1",),
    )
    result = evaluate_answer_case(
        case,
        AnswerObservation(
            case_id=case.id,
            text="Недостаточно информации о версии 2.1.",
            actual_abstention=True,
            citation_valid=True,
            cited_source_text="",
        ),
    )

    assert result.required_fact_coverage == 0.0
    assert result.exact_value_correct is False
    assert result.citation_fact_coverage is None
    assert result.citation_source_coverage is None


def test_retrieval_metrics_are_undefined_for_abstention_case() -> None:
    case = EvaluationCase(
        id="no-answer-001",
        question="Какой код отсутствует в базе?",
        expected_abstention=True,
    )
    observation = RetrievalObservation(
        case_id=case.id,
        retrieved_document_ids=(),
        actual_abstention=True,
    )

    result = evaluate_retrieval_case(case, observation)

    assert result.recall_at_1 is None
    assert result.recall_at_5 is None
    assert result.recall_at_10 is None
    assert result.reciprocal_rank is None
    assert result.ndcg_at_10 is None
    assert result.abstention_correct is True


def test_aggregate_metrics_exclude_undefined_retrieval_values() -> None:
    retrieval_case = EvaluationCase(
        id="retrieval-001",
        question="Какой статус у проекта?",
        expected_document_ids=("document-a",),
    )
    retrieval_observation = RetrievalObservation(
        case_id=retrieval_case.id,
        retrieved_document_ids=("document-b", "document-a"),
        actual_abstention=False,
    )

    abstention_case = EvaluationCase(
        id="no-answer-001",
        question="Какой информации нет в базе?",
        expected_abstention=True,
    )
    abstention_observation = RetrievalObservation(
        case_id=abstention_case.id,
        retrieved_document_ids=(),
        actual_abstention=True,
    )

    aggregate = aggregate_retrieval_metrics(
        [
            evaluate_retrieval_case(
                retrieval_case,
                retrieval_observation,
            ),
            evaluate_retrieval_case(
                abstention_case,
                abstention_observation,
            ),
        ]
    )

    assert aggregate.total_cases == 2
    assert aggregate.retrieval_evaluated_cases == 1
    assert aggregate.abstention_evaluated_cases == 2
    assert aggregate.recall_at_1 == 0.0
    assert aggregate.recall_at_5 == 1.0
    assert aggregate.recall_at_10 == 1.0
    assert aggregate.mrr == 0.5
    assert aggregate.ndcg_at_10 == pytest.approx(1 / math.log2(3))
    assert aggregate.abstention_accuracy == 1.0


def test_case_rejects_documents_for_expected_abstention() -> None:
    with pytest.raises(ValueError):
        EvaluationCase(
            id="invalid-case",
            question="Вопрос",
            expected_document_ids=("document-a",),
            expected_abstention=True,
        )


def test_observation_must_match_case_id() -> None:
    case = EvaluationCase(
        id="case-a",
        question="Вопрос",
        expected_document_ids=("document-a",),
    )
    observation = RetrievalObservation(
        case_id="case-b",
        retrieved_document_ids=("document-a",),
    )

    with pytest.raises(ValueError):
        evaluate_retrieval_case(case, observation)
