from __future__ import annotations

import pytest

from app.evaluation.metrics import (
    aggregate_retrieval_metrics,
    evaluate_retrieval_case,
    recall_at_k,
    reciprocal_rank,
)
from app.evaluation.models import (
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
    assert result.reciprocal_rank is None
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
    assert aggregate.mrr == 0.5
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
