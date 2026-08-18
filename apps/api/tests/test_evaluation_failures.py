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
        "context_missing_required_facts",
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
    assert report["summary"]["failing_case_count"] == 1
    assert report["summary"]["failure_count"] == 6


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
