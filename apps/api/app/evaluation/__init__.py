"""Deterministic evaluation primitives for the RAG pipeline."""

from app.evaluation.dataset import DatasetError, load_cases
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
    AnswerAggregateMetrics,
    AnswerCaseMetrics,
    AnswerObservation,
    EvaluationCase,
    RetrievalAggregateMetrics,
    RetrievalCaseMetrics,
    RetrievalObservation,
)

__all__ = [
    "AnswerAggregateMetrics",
    "AnswerCaseMetrics",
    "AnswerObservation",
    "DatasetError",
    "EvaluationCase",
    "RetrievalAggregateMetrics",
    "RetrievalCaseMetrics",
    "RetrievalObservation",
    "aggregate_answer_metrics",
    "aggregate_retrieval_metrics",
    "evaluate_answer_case",
    "evaluate_retrieval_case",
    "fact_coverage",
    "load_cases",
    "ndcg_at_k",
    "normalize_for_match",
    "recall_at_k",
    "reciprocal_rank",
]
