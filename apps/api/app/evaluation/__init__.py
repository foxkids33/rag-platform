"""Deterministic evaluation primitives for the RAG pipeline."""

from app.evaluation.metrics import (
    aggregate_retrieval_metrics,
    evaluate_retrieval_case,
    reciprocal_rank,
    recall_at_k,
)
from app.evaluation.models import (
    EvaluationCase,
    RetrievalAggregateMetrics,
    RetrievalCaseMetrics,
    RetrievalObservation,
)

__all__ = [
    "EvaluationCase",
    "RetrievalAggregateMetrics",
    "RetrievalCaseMetrics",
    "RetrievalObservation",
    "aggregate_retrieval_metrics",
    "evaluate_retrieval_case",
    "reciprocal_rank",
    "recall_at_k",
]
