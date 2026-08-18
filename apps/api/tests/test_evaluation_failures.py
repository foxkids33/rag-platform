from __future__ import annotations

from app.evaluation.failures import build_failure_report, render_markdown


def _case() -> dict:
    return {
        "case": {
            "id": "exact-001",
            "question": "Какая версия?",
            "question_type": "exact_value",
            "difficulty": "hard",
            "expected_abstention": False,
            "expected_filenames": ["review.txt"],
        },
        "retrieved_filenames": ["other.txt", "review.txt"],
        "metrics": {
            "recall_at_1": 0.0,
            "recall_at_10": 1.0,
            "abstention_correct": True,
        },
        "answer_metrics": {
            "required_fact_coverage": 0.5,
            "exact_value_correct": False,
            "forbidden_fact_violation": False,
            "context_fact_coverage": 0.5,
            "context_source_coverage": 1.0,
            "gold_context_fact_coverage": 0.5,
            "citation_fact_coverage": 0.0,
            "citation_source_coverage": 1.0,
            "citation_valid": False,
        },
        "search": {
            "latency_ms": 80.0,
            "rerank_applied": False,
            "result_count": 2,
        },
        "answer": {
            "latency_ms": 7000.0,
            "abstained": False,
            "abstention_reason": None,
            "sources": [{"filename": "review.txt"}],
        },
    }


def test_failure_report_classifies_pipeline_layers() -> None:
    source = {
        "schema_version": 3,
        "configuration": {"rerank": False},
        "cases": [_case()],
        "errors": [],
    }

    report = build_failure_report(source)

    failure = report["failures"][0]
    assert failure["categories"] == [
        "relevant_not_ranked_first",
        "missing_required_facts",
        "wrong_exact_value",
        "invalid_citation",
        "gold_source_chunk_missing_required_facts",
        "context_missing_required_facts",
        "citation_missing_required_facts",
        "slow_answer",
    ]
    assert failure["layers"] == [
        "citations",
        "context",
        "generation",
        "latency",
        "retrieval",
    ]
    assert failure["abstention_reason"] is None
    assert failure["context_filenames"] == ["review.txt"]
    assert failure["context_fact_coverage"] == 0.5
    assert report["summary"]["failing_case_count"] == 1
    assert report["summary"]["failure_count"] == 8


def test_failure_report_separates_model_abstention_from_missing_context() -> None:
    result = _case()
    result["metrics"] = {
        "recall_at_1": 1.0,
        "recall_at_10": 1.0,
        "abstention_correct": False,
    }
    result["answer_metrics"] = {
        "required_fact_coverage": 0.0,
        "exact_value_correct": None,
        "forbidden_fact_violation": False,
        "context_fact_coverage": 1.0,
        "context_source_coverage": 1.0,
        "gold_context_fact_coverage": 1.0,
        "citation_fact_coverage": None,
        "citation_source_coverage": None,
        "citation_valid": None,
    }
    result["answer"].update(
        {
            "latency_ms": 100.0,
            "abstained": True,
            "abstention_reason": "model_reported_insufficient_context",
        }
    )

    report = build_failure_report(
        {
            "schema_version": 3,
            "configuration": {"rerank": False},
            "cases": [result],
            "errors": [],
        }
    )

    categories = report["failures"][0]["categories"]
    assert categories == [
        "false_abstention",
        "missing_required_facts",
        "model_abstained_with_available_context",
    ]
    assert "context_missing_required_facts" not in categories


def test_failure_report_distinguishes_missing_gold_source_from_wrong_chunk() -> None:
    result = _case()
    result["answer_metrics"].update(
        {
            "context_fact_coverage": 0.0,
            "context_source_coverage": 0.0,
            "gold_context_fact_coverage": 0.0,
        }
    )
    result["answer"]["sources"] = [{"filename": "other.txt"}]

    report = build_failure_report(
        {
            "schema_version": 3,
            "configuration": {"rerank": False},
            "cases": [result],
            "errors": [],
        }
    )

    failure = report["failures"][0]
    assert "gold_source_missing_from_context" in failure["categories"]
    assert "gold_source_chunk_missing_required_facts" not in failure["categories"]
    assert failure["context_filenames"] == ["other.txt"]


def test_failure_markdown_contains_case_and_categories() -> None:
    report = build_failure_report(
        {
            "schema_version": 3,
            "configuration": {"rerank": False},
            "cases": [_case()],
            "errors": [],
        }
    )

    markdown = render_markdown(report)

    assert "# Evaluation failure report" in markdown
    assert "exact-001" in markdown
    assert "wrong_exact_value" in markdown
