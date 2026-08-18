"""Data models used by deterministic evaluation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EvaluationCase:
    """Expected outcome for one evaluation question."""

    id: str
    question: str
    expected_document_ids: tuple[str, ...] = ()
    expected_filenames: tuple[str, ...] = ()
    expected_abstention: bool = False
    question_type: str | None = None
    expected_answer: str | None = None
    required_facts: tuple[str, ...] = ()
    forbidden_facts: tuple[str, ...] = ()
    difficulty: str | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise ValueError("Evaluation case id must not be empty")

        if not self.question.strip():
            raise ValueError("Evaluation question must not be empty")

        if len(set(self.expected_document_ids)) != len(self.expected_document_ids):
            raise ValueError("expected_document_ids must not contain duplicates")

        if len(set(self.expected_filenames)) != len(self.expected_filenames):
            raise ValueError("expected_filenames must not contain duplicates")

        if self.expected_document_ids and self.expected_filenames:
            raise ValueError("Use expected_document_ids or expected_filenames, not both")

        if self.expected_abstention and (self.expected_document_ids or self.expected_filenames):
            raise ValueError("An abstention case must not define expected documents")


@dataclass(frozen=True, slots=True)
class RetrievalObservation:
    """Actual retrieval output collected for an evaluation case."""

    case_id: str
    retrieved_document_ids: tuple[str, ...]
    actual_abstention: bool | None = None


@dataclass(frozen=True, slots=True)
class RetrievalCaseMetrics:
    """Metrics calculated for one evaluation case."""

    case_id: str
    recall_at_1: float | None
    recall_at_5: float | None
    recall_at_10: float | None
    reciprocal_rank: float | None
    ndcg_at_10: float | None
    abstention_correct: bool | None


@dataclass(frozen=True, slots=True)
class RetrievalAggregateMetrics:
    """Metrics aggregated across an evaluation run."""

    total_cases: int
    retrieval_evaluated_cases: int
    abstention_evaluated_cases: int
    recall_at_1: float | None
    recall_at_5: float | None
    recall_at_10: float | None
    mrr: float | None
    ndcg_at_10: float | None
    abstention_accuracy: float | None


@dataclass(frozen=True, slots=True)
class AnswerObservation:
    """Answer and cited-source text collected from the live API."""

    case_id: str
    text: str
    actual_abstention: bool
    citation_valid: bool | None
    cited_source_text: str
    cited_document_ids: tuple[str, ...] = ()
    cited_filenames: tuple[str, ...] = ()
    context_source_text: str = ""
    context_document_ids: tuple[str, ...] = ()
    context_filenames: tuple[str, ...] = ()
    gold_context_source_text: str = ""


@dataclass(frozen=True, slots=True)
class AnswerCaseMetrics:
    """Deterministic answer-quality checks for one evaluation case."""

    case_id: str
    required_fact_coverage: float | None
    exact_value_correct: bool | None
    forbidden_fact_violation: bool | None
    context_fact_coverage: float | None
    context_source_coverage: float | None
    gold_context_fact_coverage: float | None
    citation_fact_coverage: float | None
    citation_source_coverage: float | None
    citation_valid: bool | None


@dataclass(frozen=True, slots=True)
class AnswerAggregateMetrics:
    """Aggregate deterministic answer-quality metrics."""

    total_cases: int
    fact_evaluated_cases: int
    exact_value_evaluated_cases: int
    forbidden_fact_evaluated_cases: int
    context_fact_evaluated_cases: int
    context_source_evaluated_cases: int
    gold_context_fact_evaluated_cases: int
    citation_fact_evaluated_cases: int
    citation_source_evaluated_cases: int
    citation_validity_evaluated_cases: int
    required_fact_coverage: float | None
    exact_value_accuracy: float | None
    forbidden_fact_violation_rate: float | None
    context_fact_coverage: float | None
    context_source_coverage: float | None
    gold_context_fact_coverage: float | None
    citation_fact_coverage: float | None
    citation_source_coverage: float | None
    citation_validity_rate: float | None
