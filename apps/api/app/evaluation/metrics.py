"""Deterministic retrieval evaluation metrics."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from statistics import fmean

from app.evaluation.models import (
    EvaluationCase,
    RetrievalAggregateMetrics,
    RetrievalCaseMetrics,
    RetrievalObservation,
)


def recall_at_k(
    expected_document_ids: Iterable[str],
    retrieved_document_ids: Sequence[str],
    k: int,
) -> float | None:
    """Calculate document-level Recall@K.

    Returns ``None`` when the case has no expected documents, because
    retrieval recall is not defined for an abstention-only case.
    """

    if k < 1:
        raise ValueError("k must be greater than or equal to 1")

    expected = set(expected_document_ids)

    if not expected:
        return None

    retrieved = set(retrieved_document_ids[:k])
    return len(expected & retrieved) / len(expected)


def reciprocal_rank(
    expected_document_ids: Iterable[str],
    retrieved_document_ids: Sequence[str],
) -> float | None:
    """Return reciprocal rank of the first relevant document."""

    expected = set(expected_document_ids)

    if not expected:
        return None

    for rank, document_id in enumerate(
        retrieved_document_ids,
        start=1,
    ):
        if document_id in expected:
            return 1.0 / rank

    return 0.0


def evaluate_retrieval_case(
    case: EvaluationCase,
    observation: RetrievalObservation,
) -> RetrievalCaseMetrics:
    """Calculate deterministic metrics for one evaluation case."""

    if observation.case_id != case.id:
        raise ValueError(
            "Observation case_id does not match evaluation case id"
        )

    abstention_correct: bool | None = None

    if observation.actual_abstention is not None:
        abstention_correct = (
            observation.actual_abstention
            == case.expected_abstention
        )

    return RetrievalCaseMetrics(
        case_id=case.id,
        recall_at_1=recall_at_k(
            case.expected_document_ids,
            observation.retrieved_document_ids,
            k=1,
        ),
        recall_at_5=recall_at_k(
            case.expected_document_ids,
            observation.retrieved_document_ids,
            k=5,
        ),
        reciprocal_rank=reciprocal_rank(
            case.expected_document_ids,
            observation.retrieved_document_ids,
        ),
        abstention_correct=abstention_correct,
    )


def _mean_or_none(values: Iterable[float]) -> float | None:
    collected = list(values)

    if not collected:
        return None

    return fmean(collected)


def aggregate_retrieval_metrics(
    results: Iterable[RetrievalCaseMetrics],
) -> RetrievalAggregateMetrics:
    """Aggregate metrics from a complete evaluation run."""

    collected = list(results)

    recall_at_1_values = [
        result.recall_at_1
        for result in collected
        if result.recall_at_1 is not None
    ]
    recall_at_5_values = [
        result.recall_at_5
        for result in collected
        if result.recall_at_5 is not None
    ]
    reciprocal_rank_values = [
        result.reciprocal_rank
        for result in collected
        if result.reciprocal_rank is not None
    ]
    abstention_values = [
        float(result.abstention_correct)
        for result in collected
        if result.abstention_correct is not None
    ]

    return RetrievalAggregateMetrics(
        total_cases=len(collected),
        retrieval_evaluated_cases=len(recall_at_5_values),
        abstention_evaluated_cases=len(abstention_values),
        recall_at_1=_mean_or_none(recall_at_1_values),
        recall_at_5=_mean_or_none(recall_at_5_values),
        mrr=_mean_or_none(reciprocal_rank_values),
        abstention_accuracy=_mean_or_none(abstention_values),
    )
