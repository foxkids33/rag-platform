"""Run deterministic retrieval and answer checks against a live API."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any
from uuid import UUID

import httpx

from app.evaluation.dataset import DatasetError, load_case_ids, load_cases, select_cases
from app.evaluation.metrics import (
    aggregate_answer_metrics,
    aggregate_retrieval_metrics,
    evaluate_answer_case,
    evaluate_retrieval_case,
)
from app.evaluation.models import (
    AnswerCaseMetrics,
    AnswerObservation,
    EvaluationCase,
    RetrievalCaseMetrics,
    RetrievalObservation,
)


def _unique(values: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _cited_source_text(answer_payload: dict[str, Any]) -> str:
    cited_indices = {int(value) for value in answer_payload.get("cited_source_indices", [])}
    return "\n\n".join(
        str(source.get("excerpt", ""))
        for source in answer_payload.get("sources", [])
        if source.get("index") in cited_indices
    )


def _cited_sources(answer_payload: dict[str, Any]) -> list[dict[str, Any]]:
    cited_indices = {int(value) for value in answer_payload.get("cited_source_indices", [])}
    return [
        source
        for source in answer_payload.get("sources", [])
        if source.get("index") in cited_indices
    ]


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * fraction, 2)


def _latency_summary(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    search_values = [float(item["search"]["latency_ms"]) for item in case_results]
    answer_values = [
        float(item["answer"]["latency_ms"])
        for item in case_results
        if item["answer"]["latency_ms"] is not None
    ]
    return {
        "search": {
            "count": len(search_values),
            "p50_ms": _percentile(search_values, 0.50),
            "p95_ms": _percentile(search_values, 0.95),
        },
        "answer": {
            "count": len(answer_values),
            "p50_ms": _percentile(answer_values, 0.50),
            "p95_ms": _percentile(answer_values, 0.95),
        },
    }


def _aggregate_case_results(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    retrieval = aggregate_retrieval_metrics(
        RetrievalCaseMetrics(**item["metrics"]) for item in case_results
    )
    answer = aggregate_answer_metrics(
        AnswerCaseMetrics(**item["answer_metrics"])
        for item in case_results
        if item["answer_metrics"] is not None
    )
    retrieval_payload = asdict(retrieval)
    answerable = [
        float(item["metrics"]["abstention_correct"])
        for item in case_results
        if not item["case"]["expected_abstention"]
        and item["metrics"]["abstention_correct"] is not None
    ]
    unanswerable = [
        float(item["metrics"]["abstention_correct"])
        for item in case_results
        if item["case"]["expected_abstention"]
        and item["metrics"]["abstention_correct"] is not None
    ]
    answerable_accuracy = (
        sum(answerable) / len(answerable) if answerable else None
    )
    unanswerable_accuracy = (
        sum(unanswerable) / len(unanswerable) if unanswerable else None
    )
    balanced_accuracy = (
        (answerable_accuracy + unanswerable_accuracy) / 2
        if answerable_accuracy is not None and unanswerable_accuracy is not None
        else None
    )
    retrieval_payload.update(
        {
            "answerable_evaluated_cases": len(answerable),
            "unanswerable_evaluated_cases": len(unanswerable),
            "answerable_non_abstention_accuracy": answerable_accuracy,
            "unanswerable_abstention_accuracy": unanswerable_accuracy,
            "balanced_abstention_accuracy": balanced_accuracy,
        }
    )
    return {
        "case_count": len(case_results),
        "retrieval": retrieval_payload,
        "answer": asdict(answer),
    }


def _rerank_summary(case_results: list[dict[str, Any]], requested: bool) -> dict[str, Any]:
    applied = [item["search"]["rerank_applied"] is True for item in case_results]
    error_count = sum(bool(item["search"]["rerank_error"]) for item in case_results)
    applied_count = sum(applied)
    return {
        "requested": requested,
        "evaluated_cases": len(applied),
        "applied_cases": applied_count,
        "not_applied_cases": len(applied) - applied_count,
        "application_rate": applied_count / len(applied) if applied else None,
        "error_cases": error_count,
    }


def _breakdowns(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, list[dict[str, Any]]] = {}
    by_filename: dict[str, list[dict[str, Any]]] = {}
    for result in case_results:
        case = result["case"]
        by_type.setdefault(case.get("question_type") or "unspecified", []).append(result)
        for filename in case.get("expected_filenames", []):
            by_filename.setdefault(filename, []).append(result)
    return {
        "question_type": {
            key: _aggregate_case_results(values) for key, values in sorted(by_type.items())
        },
        "expected_filename": {
            key: _aggregate_case_results(values)
            for key, values in sorted(by_filename.items())
        },
    }


def _load_corpus(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DatasetError(f"Cannot read corpus manifest {path}: {exc}") from exc
    documents = payload.get("documents") if isinstance(payload, dict) else None
    if not isinstance(documents, list):
        raise DatasetError("Corpus manifest must contain a documents array")
    expected: dict[str, str] = {}
    for item in documents:
        if not isinstance(item, dict):
            raise DatasetError("Corpus manifest documents must be objects")
        filename = item.get("filename")
        sha256 = item.get("sha256")
        if not isinstance(filename, str) or not isinstance(sha256, str):
            raise DatasetError("Corpus manifest documents require filename and sha256")
        expected[filename] = sha256
    return expected


async def _preflight_documents(
    client: httpx.AsyncClient,
    workspace_id: UUID,
    cases: list[EvaluationCase],
    corpus: dict[str, str],
) -> None:
    expected_filenames = {
        filename for case in cases for filename in case.expected_filenames
    } | set(corpus)
    if not expected_filenames:
        return

    response = await client.get(f"/workspaces/{workspace_id}/documents")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise DatasetError("Document preflight returned an invalid response")

    problems: list[str] = []
    for filename in sorted(expected_filenames):
        candidates = [item for item in payload if item.get("filename") == filename]
        if not candidates:
            problems.append(f"missing: {filename}")
            continue
        expected_sha = corpus.get(filename)
        ready = [
            item
            for item in candidates
            if item.get("status") == "READY"
            and item.get("search_enabled") is True
            and (expected_sha is None or item.get("sha256") == expected_sha)
        ]
        if not ready:
            reason = "not READY/search-enabled"
            if expected_sha is not None and all(
                item.get("sha256") != expected_sha for item in candidates
            ):
                reason = "SHA-256 mismatch"
            problems.append(f"{reason}: {filename}")
    if problems:
        raise DatasetError("Document preflight failed:\n- " + "\n- ".join(problems))


async def evaluate_case(
    client: httpx.AsyncClient,
    workspace_id: UUID,
    case: EvaluationCase,
    *,
    limit: int,
    mode: str,
    rerank: bool,
    include_answers: bool,
    answer_temperature: float,
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
    abstention_reason: str | None = None
    citation_valid: bool | None = None
    answer_text: str | None = None
    answer_latency_ms: float | None = None
    generation_temperature: float | None = None
    answer_sources: list[dict[str, Any]] = []
    answer_metrics: AnswerCaseMetrics | None = None

    if include_answers:
        answer_started = perf_counter()
        answer_response = await client.post(
            f"/workspaces/{workspace_id}/answer",
            json={
                "question": case.question,
                "retrieval_limit": limit,
                "mode": mode,
                "rerank": rerank,
                "temperature": answer_temperature,
            },
        )
        answer_response.raise_for_status()
        answer_payload = answer_response.json()
        answer_latency_ms = round((perf_counter() - answer_started) * 1000, 2)
        actual_abstention = bool(answer_payload["abstained"])
        abstention_reason = answer_payload.get("abstention_reason")
        citation_valid = answer_payload.get("citation_valid")
        answer_text = answer_payload.get("answer")
        generation_temperature = answer_payload.get("generation_temperature")
        answer_sources = answer_payload.get("sources", [])
        cited_sources = _cited_sources(answer_payload)
        answer_metrics = evaluate_answer_case(
            case,
            AnswerObservation(
                case_id=case.id,
                text=str(answer_text or ""),
                actual_abstention=actual_abstention,
                citation_valid=citation_valid,
                cited_source_text=_cited_source_text(answer_payload),
                cited_document_ids=_unique(
                    [str(source.get("document_id", "")) for source in cited_sources]
                ),
                cited_filenames=_unique(
                    [str(source.get("filename", "")) for source in cited_sources]
                ),
            ),
        )

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
        "answer_metrics": asdict(answer_metrics) if answer_metrics is not None else None,
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
            "abstention_reason": abstention_reason,
            "citation_valid": citation_valid,
            "generation_temperature": generation_temperature,
            "text": answer_text,
            "sources": answer_sources,
        },
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    cases = load_cases(args.dataset)
    if args.case_ids is not None:
        cases = select_cases(cases, load_case_ids(args.case_ids))
    started_at = datetime.now(UTC)
    case_results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    timeout = httpx.Timeout(args.timeout)
    token = args.token or os.getenv("RAG_API_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else None
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/") + "/api/v1",
        timeout=timeout,
        headers=headers,
    ) as client:
        await _preflight_documents(
            client,
            args.workspace_id,
            cases,
            _load_corpus(args.corpus),
        )
        for index, case in enumerate(cases, start=1):
            print(f"[{index}/{len(cases)}] {case.id}", flush=True)
            try:
                result = await evaluate_case(
                    client,
                    args.workspace_id,
                    case,
                    limit=args.limit,
                    mode=args.mode,
                    rerank=args.rerank,
                    include_answers=args.answers,
                    answer_temperature=args.answer_temperature,
                )
            except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                errors.append({"case_id": case.id, "error": str(exc)})
                print(f"  failed: {exc}", flush=True)
                if args.fail_fast:
                    raise
            else:
                case_results.append(result)
                print("  ok", flush=True)

    aggregate = _aggregate_case_results(case_results)
    rerank_summary = _rerank_summary(case_results, args.rerank)
    warnings: list[str] = []
    if args.rerank and rerank_summary["not_applied_cases"]:
        warnings.append(
            "Reranking was requested but was not applied to "
            f"{rerank_summary['not_applied_cases']} case(s)."
        )

    return {
        "schema_version": 3,
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(UTC).isoformat(),
        "configuration": {
            "base_url": args.base_url,
            "workspace_id": str(args.workspace_id),
            "dataset": str(args.dataset),
            "case_ids": str(args.case_ids) if args.case_ids else None,
            "corpus": str(args.corpus) if args.corpus else None,
            "limit": args.limit,
            "mode": args.mode,
            "rerank": args.rerank,
            "answers": args.answers,
            "answer_temperature": args.answer_temperature,
            "case_details_included": not args.omit_case_details,
        },
        "requested_case_count": len(cases),
        "successful_case_count": len(case_results),
        "failed_case_count": len(errors),
        "aggregate": aggregate,
        "execution": {"rerank": rerank_summary},
        "latency": _latency_summary(case_results),
        "breakdown": _breakdowns(case_results),
        "errors": errors,
        "warnings": warnings,
        "cases": [] if args.omit_case_details else case_results,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument(
        "--case-ids",
        type=Path,
        help="Optional file containing the case ids for a dev or acceptance split",
    )
    parser.add_argument("--corpus", type=Path)
    parser.add_argument("--workspace-id", type=UUID, required=True)
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--mode", choices=("hybrid", "semantic", "lexical"), default="hybrid")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument(
        "--answer-temperature",
        type=float,
        default=0.0,
        help="Generation temperature used by answer evaluation requests",
    )
    parser.add_argument(
        "--token",
        help="OIDC access token; prefer the RAG_API_TOKEN environment variable",
    )
    parser.add_argument("--answers", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--rerank", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument(
        "--omit-case-details",
        action="store_true",
        help="Keep aggregate metrics but omit questions and per-case results",
    )
    parser.add_argument("--fail-fast", action="store_true")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 20:
        parser.error("--limit must be between 1 and 20")
    if args.answer_temperature < 0.0 or args.answer_temperature > 2.0:
        parser.error("--answer-temperature must be between 0 and 2")

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
    print(
        json.dumps(
            {
                "aggregate": report["aggregate"],
                "execution": report["execution"],
                "latency": report["latency"],
                "warnings": report["warnings"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(f"Report: {args.output}")
    return 0 if report["failed_case_count"] == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
