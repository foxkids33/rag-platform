"""Deterministic evaluation primitives for the RAG pipeline."""

from app.evaluation.dataset import DatasetError, load_cases
from app.evaluation.metrics import (
    aggregate_retrieval_metrics,
    evaluate_retrieval_case,
    recall_at_k,
    reciprocal_rank,
)
from app.evaluation.models import (
    EvaluationCase,
    RetrievalAggregateMetrics,
    RetrievalCaseMetrics,
    RetrievalObservation,
)

__all__ = [
    "DatasetError",
    "EvaluationCase",
    "RetrievalAggregateMetrics",
    "RetrievalCaseMetrics",
    "RetrievalObservation",
    "aggregate_retrieval_metrics",
    "evaluate_retrieval_case",
    "load_cases",
    "recall_at_k",
    "reciprocal_rank",
]
