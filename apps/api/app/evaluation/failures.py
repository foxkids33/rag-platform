"""Turn a schema-v3 evaluation report into an actionable failure inventory."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


class FailureReportError(ValueError):
    pass


CATEGORY_LAYERS = {
    "empty_retrieval": "retrieval",
    "retrieval_incomplete_at_10": "retrieval",
    "relevant_not_ranked_first": "retrieval",
    "rerank_not_applied": "reranker",
    "false_abstention": "generation",
    "missed_abstention": "generation",
    "missing_required_facts": "generation",
    "wrong_exact_value": "generation",
    "forbidden_fact": "generation",
    "invalid_citation": "citations",
    "context_missing_required_facts": "context",
    "citation_missing_gold_source": "citations",
    "slow_search": "latency",
    "slow_answer": "latency",
    "evaluation_error": "execution",
}


def _load_report(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FailureReportError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 3:
        raise FailureReportError("Failure analysis requires a schema-v3 report")
    if not isinstance(payload.get("cases"), list):
        raise FailureReportError("Evaluation report must contain a cases array")
    return payload


def _below(value: Any, threshold: float) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value < threshold


def _above(value: Any, threshold: float) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and value > threshold


def _case_categories(
    result: dict[str, Any],
    *,
    rerank_requested: bool,
    search_latency_ms: float,
    answer_latency_ms: float,
) -> list[str]:
    case = result.get("case", {})
    metrics = result.get("metrics", {})
    answer_metrics = result.get("answer_metrics") or {}
    search = result.get("search", {})
    answer = result.get("answer", {})
    categories: list[str] = []

    if search.get("result_count") == 0:
        categories.append("empty_retrieval")
    if _below(metrics.get("recall_at_10"), 1.0):
        categories.append("retrieval_incomplete_at_10")
    if _below(metrics.get("recall_at_1"), 1.0) and not _below(
        metrics.get("recall_at_10"),
        0.000001,
    ):
        categories.append("relevant_not_ranked_first")
    if rerank_requested and search.get("rerank_applied") is not True:
        categories.append("rerank_not_applied")

    if metrics.get("abstention_correct") is False:
        if case.get("expected_abstention") is True and answer.get("abstained") is False:
            categories.append("missed_abstention")
        elif case.get("expected_abstention") is False and answer.get("abstained") is True:
            categories.append("false_abstention")

    if _below(answer_metrics.get("required_fact_coverage"), 1.0):
        categories.append("missing_required_facts")
    if answer_metrics.get("exact_value_correct") is False:
        categories.append("wrong_exact_value")
    if answer_metrics.get("forbidden_fact_violation") is True:
        categories.append("forbidden_fact")
    if answer_metrics.get("citation_valid") is False:
        categories.append("invalid_citation")
    if _below(answer_metrics.get("citation_fact_coverage"), 1.0):
        categories.append("context_missing_required_facts")
    if _below(answer_metrics.get("citation_source_coverage"), 1.0):
        categories.append("citation_missing_gold_source")
    if _above(search.get("latency_ms"), search_latency_ms):
        categories.append("slow_search")
    if _above(answer.get("latency_ms"), answer_latency_ms):
        categories.append("slow_answer")
    return categories


def build_failure_report(
    report: dict[str, Any],
    *,
    search_latency_ms: float = 150.0,
    answer_latency_ms: float = 6000.0,
) -> dict[str, Any]:
    cases = report.get("cases")
    if not isinstance(cases, list) or not cases:
        raise FailureReportError(
            "Report has no case details; run the dev evaluation without "
            "--omit-case-details"
        )

    configuration = report.get("configuration", {})
    rerank_requested = configuration.get("rerank") is True
    failures: list[dict[str, Any]] = []
    category_counts: Counter[str] = Counter()
    layer_counts: Counter[str] = Counter()
    question_type_counts: Counter[str] = Counter()

    for result in cases:
        categories = _case_categories(
            result,
            rerank_requested=rerank_requested,
            search_latency_ms=search_latency_ms,
            answer_latency_ms=answer_latency_ms,
        )
        if not categories:
            continue
        case = result.get("case", {})
        layers = sorted({CATEGORY_LAYERS[category] for category in categories})
        category_counts.update(categories)
        layer_counts.update(layers)
        question_type = case.get("question_type") or "unspecified"
        question_type_counts[question_type] += 1
        failures.append(
            {
                "case_id": case.get("id"),
                "question": case.get("question"),
                "question_type": question_type,
                "difficulty": case.get("difficulty"),
                "layers": layers,
                "categories": categories,
                "expected_filenames": case.get("expected_filenames", []),
                "retrieved_filenames": result.get("retrieved_filenames", []),
                "metrics": result.get("metrics"),
                "answer_metrics": result.get("answer_metrics"),
                "abstention_reason": result.get("answer", {}).get(
                    "abstention_reason"
                ),
                "search_latency_ms": result.get("search", {}).get("latency_ms"),
                "answer_latency_ms": result.get("answer", {}).get("latency_ms"),
            }
        )

    for error in report.get("errors", []):
        category_counts["evaluation_error"] += 1
        layer_counts["execution"] += 1
        failures.append(
            {
                "case_id": error.get("case_id"),
                "question": None,
                "question_type": "unknown",
                "difficulty": None,
                "layers": ["execution"],
                "categories": ["evaluation_error"],
                "error": error.get("error"),
            }
        )

    analyzed_count = len(cases) + len(report.get("errors", []))
    return {
        "schema_version": 1,
        "source_report_schema_version": report.get("schema_version"),
        "configuration": configuration,
        "thresholds": {
            "search_latency_ms": search_latency_ms,
            "answer_latency_ms": answer_latency_ms,
        },
        "summary": {
            "analyzed_case_count": analyzed_count,
            "passing_case_count": analyzed_count - len(failures),
            "failing_case_count": len(failures),
            "failure_count": sum(category_counts.values()),
            "category_counts": dict(sorted(category_counts.items())),
            "layer_counts": dict(sorted(layer_counts.items())),
            "failing_cases_by_question_type": dict(
                sorted(question_type_counts.items())
            ),
        },
        "failures": failures,
    }


def _markdown_cell(value: Any, max_chars: int = 120) -> str:
    rendered = "" if value is None else str(value)
    rendered = " ".join(rendered.split()).replace("|", "\\|")
    return rendered if len(rendered) <= max_chars else rendered[: max_chars - 1] + "…"


def render_markdown(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "# Evaluation failure report",
        "",
        f"- Analyzed cases: {summary['analyzed_case_count']}",
        f"- Passing cases: {summary['passing_case_count']}",
        f"- Failing cases: {summary['failing_case_count']}",
        f"- Failure signals: {summary['failure_count']}",
        "",
        "## Failure categories",
        "",
        "| Category | Count |",
        "|---|---:|",
    ]
    lines.extend(
        f"| `{category}` | {count} |"
        for category, count in summary["category_counts"].items()
    )
    lines.extend(
        [
            "",
            "## Failing cases",
            "",
            "| Case | Type | Layers | Categories | Expected | Retrieved | Question |",
            "|---|---|---|---|---|---|---|",
        ]
    )
    for failure in report["failures"]:
        lines.append(
            "| "
            + " | ".join(
                [
                    _markdown_cell(failure.get("case_id")),
                    _markdown_cell(failure.get("question_type")),
                    _markdown_cell(", ".join(failure.get("layers", []))),
                    _markdown_cell(", ".join(failure.get("categories", []))),
                    _markdown_cell(", ".join(failure.get("expected_filenames", []))),
                    _markdown_cell(", ".join(failure.get("retrieved_filenames", []))),
                    _markdown_cell(failure.get("question")),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--markdown", type=Path)
    parser.add_argument("--search-latency-ms", type=float, default=150.0)
    parser.add_argument("--answer-latency-ms", type=float, default=6000.0)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.search_latency_ms <= 0 or args.answer_latency_ms <= 0:
        parser.error("latency thresholds must be positive")
    try:
        failure_report = build_failure_report(
            _load_report(args.report),
            search_latency_ms=args.search_latency_ms,
            answer_latency_ms=args.answer_latency_ms,
        )
    except FailureReportError as exc:
        print(f"Failure analysis failed: {exc}", file=sys.stderr)
        return 2

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(failure_report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(failure_report), encoding="utf-8")
    print(json.dumps(failure_report["summary"], ensure_ascii=False, indent=2))
    print(f"Failure report: {args.output}")
    if args.markdown:
        print(f"Markdown report: {args.markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
