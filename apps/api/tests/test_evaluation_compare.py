from __future__ import annotations

from app.evaluation.compare import compare_reports


def _report(*, recall_at_5: float, p95_ms: float) -> dict:
    return {
        "aggregate": {
            "retrieval": {"recall_at_5": recall_at_5},
            "answer": {},
        },
        "latency": {"search": {"p95_ms": p95_ms}, "answer": {}},
    }


def test_compare_reports_accepts_small_regression() -> None:
    result = compare_reports(
        _report(recall_at_5=0.89, p95_ms=110),
        baseline=_report(recall_at_5=0.90, p95_ms=100),
    )

    assert result["passed"] is True


def test_compare_reports_rejects_quality_and_gate_regression() -> None:
    result = compare_reports(
        _report(recall_at_5=0.80, p95_ms=150),
        baseline=_report(recall_at_5=0.90, p95_ms=100),
        gates={"minimum": {"aggregate.retrieval.recall_at_5": 0.9}},
    )

    assert result["passed"] is False
    assert result["failed_check_count"] == 3


def test_compare_reports_checks_cited_gold_document_coverage() -> None:
    baseline = _report(recall_at_5=0.90, p95_ms=100)
    candidate = _report(recall_at_5=0.90, p95_ms=100)
    baseline["aggregate"]["answer"]["citation_source_coverage"] = 0.8
    candidate["aggregate"]["answer"]["citation_source_coverage"] = 0.6

    result = compare_reports(candidate, baseline=baseline)

    assert result["passed"] is False
    assert any(
        check["path"] == "aggregate.answer.citation_source_coverage"
        and check["passed"] is False
        for check in result["checks"]
    )
