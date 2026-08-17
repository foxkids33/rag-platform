"""Compare an evaluation report with a baseline and optional quality gates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

MISSING = object()

HIGHER_IS_BETTER = (
    "aggregate.retrieval.recall_at_1",
    "aggregate.retrieval.recall_at_5",
    "aggregate.retrieval.recall_at_10",
    "aggregate.retrieval.mrr",
    "aggregate.retrieval.ndcg_at_10",
    "aggregate.retrieval.abstention_accuracy",
    "aggregate.retrieval.answerable_non_abstention_accuracy",
    "aggregate.retrieval.unanswerable_abstention_accuracy",
    "aggregate.retrieval.balanced_abstention_accuracy",
    "aggregate.answer.required_fact_coverage",
    "aggregate.answer.exact_value_accuracy",
    "aggregate.answer.citation_fact_coverage",
    "aggregate.answer.citation_source_coverage",
    "aggregate.answer.citation_validity_rate",
    "execution.rerank.application_rate",
)
LOWER_IS_BETTER = (
    "aggregate.answer.forbidden_fact_violation_rate",
)
LATENCY_PATHS = (
    "latency.search.p50_ms",
    "latency.search.p95_ms",
    "latency.answer.p50_ms",
    "latency.answer.p95_ms",
)


class ComparisonError(ValueError):
    pass


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ComparisonError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ComparisonError(f"{path} must contain a JSON object")
    return payload


def _raw_value(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return MISSING
        current = current[part]
    return current


def _value(payload: dict[str, Any], path: str) -> float | None:
    current = _raw_value(payload, path)
    if current is MISSING:
        return None
    if current is None:
        return None
    if not isinstance(current, int | float) or isinstance(current, bool):
        raise ComparisonError(f"{path} must be numeric or null")
    return float(current)


def _baseline_checks(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    max_quality_regression: float,
    max_latency_regression: float,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for path in HIGHER_IS_BETTER + LOWER_IS_BETTER:
        before = _value(baseline, path)
        after = _value(candidate, path)
        if before is None or after is None:
            continue
        delta = after - before
        passed = (
            delta >= -max_quality_regression
            if path in HIGHER_IS_BETTER
            else delta <= max_quality_regression
        )
        checks.append(
            {
                "kind": "baseline",
                "path": path,
                "baseline": before,
                "candidate": after,
                "delta": round(delta, 6),
                "passed": passed,
            }
        )

    for path in LATENCY_PATHS:
        before = _value(baseline, path)
        after = _value(candidate, path)
        if before is None or after is None:
            continue
        allowed = before * (1.0 + max_latency_regression)
        checks.append(
            {
                "kind": "latency_baseline",
                "path": path,
                "baseline": before,
                "candidate": after,
                "maximum": round(allowed, 2),
                "passed": after <= allowed,
            }
        )
    return checks


def _gate_checks(gates: dict[str, Any], candidate: dict[str, Any]) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for kind in ("minimum", "maximum"):
        values = gates.get(kind, {})
        if not isinstance(values, dict):
            raise ComparisonError(f"gates.{kind} must be an object")
        for path, threshold in values.items():
            if not isinstance(path, str) or not isinstance(threshold, int | float):
                raise ComparisonError(f"gates.{kind} entries must be numeric")
            actual = _value(candidate, path)
            passed = actual is not None and (
                actual >= float(threshold) if kind == "minimum" else actual <= float(threshold)
            )
            checks.append(
                {
                    "kind": kind,
                    "path": path,
                    "threshold": float(threshold),
                    "candidate": actual,
                    "passed": passed,
                }
            )
    equals = gates.get("equals", {})
    if not isinstance(equals, dict):
        raise ComparisonError("gates.equals must be an object")
    for path, expected in equals.items():
        if not isinstance(path, str) or isinstance(expected, dict | list):
            raise ComparisonError("gates.equals entries must use scalar values")
        actual = _raw_value(candidate, path)
        passed = actual is not MISSING and actual == expected
        checks.append(
            {
                "kind": "equals",
                "path": path,
                "expected": expected,
                "candidate": None if actual is MISSING else actual,
                "passed": passed,
            }
        )
    return checks


def compare_reports(
    candidate: dict[str, Any],
    *,
    baseline: dict[str, Any] | None = None,
    gates: dict[str, Any] | None = None,
    max_quality_regression: float = 0.02,
    max_latency_regression: float = 0.20,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    if baseline is not None:
        checks.extend(
            _baseline_checks(
                baseline,
                candidate,
                max_quality_regression=max_quality_regression,
                max_latency_regression=max_latency_regression,
            )
        )
    if gates is not None:
        checks.extend(_gate_checks(gates, candidate))
    if not checks:
        raise ComparisonError("No comparable metrics or quality gates were found")
    failed = [check for check in checks if not check["passed"]]
    return {
        "passed": not failed,
        "check_count": len(checks),
        "failed_check_count": len(failed),
        "checks": checks,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--gates", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-quality-regression", type=float, default=0.02)
    parser.add_argument("--max-latency-regression", type=float, default=0.20)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.baseline is None and args.gates is None:
        parser.error("provide --baseline, --gates, or both")
    if not 0 <= args.max_quality_regression <= 1:
        parser.error("--max-quality-regression must be between 0 and 1")
    if args.max_latency_regression < 0:
        parser.error("--max-latency-regression must be non-negative")

    try:
        result = compare_reports(
            _load_json(args.candidate),
            baseline=_load_json(args.baseline) if args.baseline else None,
            gates=_load_json(args.gates) if args.gates else None,
            max_quality_regression=args.max_quality_regression,
            max_latency_regression=args.max_latency_regression,
        )
    except ComparisonError as exc:
        print(f"Comparison failed: {exc}", file=sys.stderr)
        return 2

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0 if result["passed"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
