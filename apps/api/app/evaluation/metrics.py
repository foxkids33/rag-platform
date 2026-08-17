"""Deterministic retrieval evaluation metrics."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable, Sequence
from statistics import fmean

from app.evaluation.models import (
    AnswerAggregateMetrics,
    AnswerCaseMetrics,
    AnswerObservation,
    EvaluationCase,
    RetrievalAggregateMetrics,
    RetrievalCaseMetrics,
    RetrievalObservation,
)

NON_WORD_RE = re.compile(r"[^0-9a-zа-я]+")
HYPHENATED_WORD_RE = re.compile(r"(?<=[0-9a-zа-я])-\s*(?=[0-9a-zа-я])")


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


def ndcg_at_k(
    expected_document_ids: Iterable[str],
    retrieved_document_ids: Sequence[str],
    k: int,
) -> float | None:
    """Calculate binary document-level nDCG@K."""

    if k < 1:
        raise ValueError("k must be greater than or equal to 1")
    expected = set(expected_document_ids)
    if not expected:
        return None

    dcg = sum(
        1.0 / math.log2(rank + 1)
        for rank, document_id in enumerate(retrieved_document_ids[:k], start=1)
        if document_id in expected
    )
    ideal_count = min(len(expected), k)
    ideal_dcg = sum(1.0 / math.log2(rank + 1) for rank in range(1, ideal_count + 1))
    return dcg / ideal_dcg


def evaluate_retrieval_case(
    case: EvaluationCase,
    observation: RetrievalObservation,
) -> RetrievalCaseMetrics:
    """Calculate deterministic metrics for one evaluation case."""

    if observation.case_id != case.id:
        raise ValueError("Observation case_id does not match evaluation case id")

    abstention_correct: bool | None = None

    if observation.actual_abstention is not None:
        abstention_correct = observation.actual_abstention == case.expected_abstention

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
        recall_at_10=recall_at_k(
            case.expected_document_ids,
            observation.retrieved_document_ids,
            k=10,
        ),
        reciprocal_rank=reciprocal_rank(
            case.expected_document_ids,
            observation.retrieved_document_ids,
        ),
        ndcg_at_10=ndcg_at_k(
            case.expected_document_ids,
            observation.retrieved_document_ids,
            k=10,
        ),
        abstention_correct=abstention_correct,
    )


def normalize_for_match(value: str) -> str:
    """Normalize typography and punctuation for deterministic fact matching."""

    normalized = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    normalized = normalized.translate(str.maketrans("‐‑‒–—−", "------"))
    normalized = HYPHENATED_WORD_RE.sub("", normalized)
    return " ".join(NON_WORD_RE.sub(" ", normalized).split())


def _contains_fact(text: str, fact: str) -> bool:
    normalized_text = normalize_for_match(text)
    alternatives = [item for item in fact.split("||") if item.strip()]
    return any(normalize_for_match(item) in normalized_text for item in alternatives)


def fact_coverage(facts: Iterable[str], text: str) -> float | None:
    expected = list(facts)
    if not expected:
        return None
    return sum(_contains_fact(text, fact) for fact in expected) / len(expected)


def citation_source_coverage(
    case: EvaluationCase,
    observation: AnswerObservation,
) -> float | None:
    """Return the share of gold documents represented by cited sources."""

    if observation.actual_abstention:
        return None
    if case.expected_filenames:
        expected = set(case.expected_filenames)
        cited = set(observation.cited_filenames)
    else:
        expected = set(case.expected_document_ids)
        cited = set(observation.cited_document_ids)
    if not expected:
        return None
    return len(expected & cited) / len(expected)


def evaluate_answer_case(
    case: EvaluationCase,
    observation: AnswerObservation,
) -> AnswerCaseMetrics:
    if observation.case_id != case.id:
        raise ValueError("Observation case_id does not match evaluation case id")

    required_fact_coverage = fact_coverage(case.required_facts, observation.text)
    if observation.actual_abstention and required_fact_coverage is not None:
        required_fact_coverage = 0.0

    exact_value_correct: bool | None = None
    if case.question_type == "exact_value":
        if observation.actual_abstention:
            exact_value_correct = False
        elif required_fact_coverage is not None:
            exact_value_correct = required_fact_coverage == 1.0
        elif case.expected_answer:
            exact_value_correct = _contains_fact(observation.text, case.expected_answer)

    forbidden_fact_violation: bool | None = None
    if case.forbidden_facts:
        forbidden_fact_violation = any(
            _contains_fact(observation.text, fact) for fact in case.forbidden_facts
        )

    return AnswerCaseMetrics(
        case_id=case.id,
        required_fact_coverage=required_fact_coverage,
        exact_value_correct=exact_value_correct,
        forbidden_fact_violation=forbidden_fact_violation,
        citation_fact_coverage=(
            fact_coverage(case.required_facts, observation.cited_source_text)
            if not observation.actual_abstention
            else None
        ),
        citation_source_coverage=citation_source_coverage(case, observation),
        citation_valid=(
            observation.citation_valid if not observation.actual_abstention else None
        ),
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
        result.recall_at_1 for result in collected if result.recall_at_1 is not None
    ]
    recall_at_5_values = [
        result.recall_at_5 for result in collected if result.recall_at_5 is not None
    ]
    recall_at_10_values = [
        result.recall_at_10 for result in collected if result.recall_at_10 is not None
    ]
    reciprocal_rank_values = [
        result.reciprocal_rank for result in collected if result.reciprocal_rank is not None
    ]
    ndcg_at_10_values = [
        result.ndcg_at_10 for result in collected if result.ndcg_at_10 is not None
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
        recall_at_10=_mean_or_none(recall_at_10_values),
        mrr=_mean_or_none(reciprocal_rank_values),
        ndcg_at_10=_mean_or_none(ndcg_at_10_values),
        abstention_accuracy=_mean_or_none(abstention_values),
    )


def aggregate_answer_metrics(
    results: Iterable[AnswerCaseMetrics],
) -> AnswerAggregateMetrics:
    collected = list(results)
    fact_values = [
        result.required_fact_coverage
        for result in collected
        if result.required_fact_coverage is not None
    ]
    exact_values = [
        float(result.exact_value_correct)
        for result in collected
        if result.exact_value_correct is not None
    ]
    forbidden_values = [
        float(result.forbidden_fact_violation)
        for result in collected
        if result.forbidden_fact_violation is not None
    ]
    citation_fact_values = [
        result.citation_fact_coverage
        for result in collected
        if result.citation_fact_coverage is not None
    ]
    citation_source_values = [
        result.citation_source_coverage
        for result in collected
        if result.citation_source_coverage is not None
    ]
    citation_validity_values = [
        float(result.citation_valid)
        for result in collected
        if result.citation_valid is not None
    ]
    return AnswerAggregateMetrics(
        total_cases=len(collected),
        fact_evaluated_cases=len(fact_values),
        exact_value_evaluated_cases=len(exact_values),
        forbidden_fact_evaluated_cases=len(forbidden_values),
        citation_fact_evaluated_cases=len(citation_fact_values),
        citation_source_evaluated_cases=len(citation_source_values),
        citation_validity_evaluated_cases=len(citation_validity_values),
        required_fact_coverage=_mean_or_none(fact_values),
        exact_value_accuracy=_mean_or_none(exact_values),
        forbidden_fact_violation_rate=_mean_or_none(forbidden_values),
        citation_fact_coverage=_mean_or_none(citation_fact_values),
        citation_source_coverage=_mean_or_none(citation_source_values),
        citation_validity_rate=_mean_or_none(citation_validity_values),
    )
