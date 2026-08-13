"""Run deterministic retrieval and answer checks against a live API."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID

import httpx

from app.evaluation.dataset import DatasetError, load_cases
from app.evaluation.metrics import aggregate_retrieval_metrics, evaluate_retrieval_case
from app.evaluation.models import (
    EvaluationCase,
    RetrievalCaseMetrics,
    RetrievalObservation,
)


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


async def evaluate_case(
    client: httpx.AsyncClient,
    workspace_id: UUID,
    case: EvaluationCase,
    *,
    limit: int,
    mode: str,
    rerank: bool,
    include_answers: bool,
) -> dict[str, Any]:
    search_started = perf_counter()
    search_response = await client.post(
        f"/workspaces/{workspace_id}/search",
        json={
            "query": case.question,
            "limit": limit,
            "mode": mode,
            "rerank": rerank,
        },
    )
    search_response.raise_for_status()
    search_payload = search_response.json()
    search_latency_ms = round((perf_counter() - search_started) * 1000, 2)

    results = search_payload.get("results", [])
    retrieved_ids = _unique([str(item["document_id"]) for item in results])
    retrieved_filenames = _unique([str(item["filename"]) for item in results])

    actual_abstention: bool | None = None
    citation_valid: bool | None = None
    answer_text: str | None = None
    answer_latency_ms: float | None = None
    answer_sources: list[dict[str, Any]] = []

    if include_answers:
        answer_started = perf_counter()
        answer_response = await client.post(
            f"/workspaces/{workspace_id}/answer",
            json={
                "question": case.question,
                "retrieval_limit": limit,
                "mode": mode,
                "rerank": rerank,
            },
        )
        answer_response.raise_for_status()
        answer_payload = answer_response.json()
        answer_latency_ms = round((perf_counter() - answer_started) * 1000, 2)
        actual_abstention = bool(answer_payload["abstained"])
        citation_valid = answer_payload.get("citation_valid")
        answer_text = answer_payload.get("answer")
        answer_sources = answer_payload.get("sources", [])

    if case.expected_filenames:
        metric_case = replace(
            case,
            expected_document_ids=case.expected_filenames,
            expected_filenames=(),
        )
        metric_retrieved = retrieved_filenames
        gold_key = "filename"
    else:
        metric_case = case
        metric_retrieved = retrieved_ids
        gold_key = "document_id"

    observation = RetrievalObservation(
        case_id=case.id,
        retrieved_document_ids=metric_retrieved,
        actual_abstention=actual_abstention,
    )
    metrics = evaluate_retrieval_case(metric_case, observation)

    return {
        "case": asdict(case),
        "gold_key": gold_key,
        "retrieved_document_ids": list(retrieved_ids),
        "retrieved_filenames": list(retrieved_filenames),
        "metrics": asdict(metrics),
        "search": {
            "latency_ms": search_latency_ms,
            "rerank_applied": search_payload.get("rerank_applied"),
            "rerank_error": search_payload.get("rerank_error"),
            "result_count": len(results),
        },
        "answer": {
            "evaluated": include_answers,
            "latency_ms": answer_latency_ms,
            "abstained": actual_abstention,
            "citation_valid": citation_valid,
            "text": answer_text,
            "sources": answer_sources,
        },
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    cases = load_cases(args.dataset)
    started_at = datetime.now(UTC)
    case_results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    timeout = httpx.Timeout(args.timeout)
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/") + "/api/v1",
        timeout=timeout,
    ) as client:
        for case in cases:
            try:
                result = await evaluate_case(
                    client,
                    args.workspace_id,
                    case,
                    limit=args.limit,
                    mode=args.mode,
                    rerank=args.rerank,
                    include_answers=args.answers,
                )
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                errors.append({"case_id": case.id, "error": str(exc)})
                if args.fail_fast:
                    raise
            else:
                case_results.append(result)

    metric_results = [RetrievalCaseMetrics(**item["metrics"]) for item in case_results]
    aggregate = aggregate_retrieval_metrics(metric_results)
    citation_values = [
        item["answer"]["citation_valid"]
        for item in case_results
        if item["answer"]["abstained"] is False and item["answer"]["citation_valid"] is not None
    ]

    return {
        "schema_version": 1,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "configuration": {
            "base_url": args.base_url,
            "workspace_id": str(args.workspace_id),
            "dataset": str(args.dataset),
            "limit": args.limit,
            "mode": args.mode,
            "rerank": args.rerank,
            "answers": args.answers,
        },
        "requested_case_count": len(cases),
        "successful_case_count": len(case_results),
        "failed_case_count": len(errors),
        "aggregate": {
            **asdict(aggregate),
            "citation_validity_rate": (
                sum(bool(value) for value in citation_values) / len(citation_values)
                if citation_values
                else None
            ),
        },
        "errors": errors,
        "cases": case_results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--mode", choices=("hybrid", "semantic", "lexical"), default="hybrid")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--answers", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--rerank", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--fail-fast", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 20:
        parser.error("--limit must be between 1 and 20")

    try:
        report = asyncio.run(run(args))
    except (DatasetError, httpx.HTTPError) as exc:
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report["aggregate"], ensure_ascii=False, indent=2))
    print(f"Report: {args.output}")
    return 0 if report["failed_case_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
