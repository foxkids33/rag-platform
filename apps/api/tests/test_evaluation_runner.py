from __future__ import annotations

import asyncio
import json
import uuid

import httpx

from app.evaluation.models import EvaluationCase
from app.evaluation.runner import _aggregate_case_results, _rerank_summary, evaluate_case


def _result_stub(*, expected_abstention: bool, abstention_correct: bool) -> dict:
    return {
        "case": {"expected_abstention": expected_abstention},
        "metrics": {
            "case_id": "stub",
            "recall_at_1": None,
            "recall_at_5": None,
            "recall_at_10": None,
            "reciprocal_rank": None,
            "ndcg_at_10": None,
            "abstention_correct": abstention_correct,
        },
        "answer_metrics": None,
        "search": {
            "rerank_applied": False,
            "rerank_error": None,
        },
    }


def test_aggregate_separates_answerable_and_unanswerable_abstention() -> None:
    results = [
        _result_stub(expected_abstention=False, abstention_correct=True),
        _result_stub(expected_abstention=True, abstention_correct=False),
    ]

    aggregate = _aggregate_case_results(results)

    assert aggregate["retrieval"]["abstention_accuracy"] == 0.5
    assert aggregate["retrieval"]["answerable_non_abstention_accuracy"] == 1.0
    assert aggregate["retrieval"]["unanswerable_abstention_accuracy"] == 0.0
    assert aggregate["retrieval"]["balanced_abstention_accuracy"] == 0.5


def test_rerank_summary_exposes_requested_but_not_applied_runs() -> None:
    summary = _rerank_summary(
        [_result_stub(expected_abstention=False, abstention_correct=True)],
        requested=True,
    )

    assert summary["requested"] is True
    assert summary["application_rate"] == 0.0
    assert summary["not_applied_cases"] == 1


def test_evaluate_case_uses_filenames_as_stable_gold_keys() -> None:
    document_id = uuid.uuid4()
    workspace_id = uuid.uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/workspaces/{workspace_id}/search")
        return httpx.Response(
            200,
            json={
                "rerank_applied": True,
                "rerank_error": None,
                "results": [
                    {
                        "document_id": str(document_id),
                        "filename": "policy.txt",
                    }
                ],
            },
        )

    async def run_case():
        async with httpx.AsyncClient(
            base_url="http://test/api/v1",
            transport=httpx.MockTransport(handler),
        ) as client:
            return await evaluate_case(
                client,
                workspace_id,
                EvaluationCase(
                    id="fact-001",
                    question="Какой срок хранения?",
                    expected_filenames=("policy.txt",),
                ),
                limit=10,
                mode="hybrid",
                rerank=True,
                include_answers=False,
                answer_temperature=0.0,
            )

    result = asyncio.run(run_case())

    assert result["gold_key"] == "filename"
    assert result["metrics"]["recall_at_1"] == 1.0
    assert result["metrics"]["recall_at_5"] == 1.0
    assert result["metrics"]["recall_at_10"] == 1.0
    assert result["metrics"]["ndcg_at_10"] == 1.0


def test_evaluate_case_scores_answer_and_only_cited_source_text() -> None:
    document_id = uuid.uuid4()
    workspace_id = uuid.uuid4()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/workspaces/{workspace_id}/search"):
            return httpx.Response(
                200,
                json={
                    "rerank_applied": False,
                    "rerank_error": None,
                    "results": [
                        {"document_id": str(document_id), "filename": "review.txt"}
                    ],
                },
            )
        request_payload = json.loads(request.content)
        assert request_payload["temperature"] == 0.0
        return httpx.Response(
            200,
            json={
                "abstained": False,
                "abstention_reason": None,
                "generation_temperature": 0.0,
                "citation_valid": True,
                "cited_source_indices": [1],
                "answer": "Версия 2.1 от 01.09.2025 [1].",
                "sources": [
                    {
                        "index": 1,
                        "document_id": str(document_id),
                        "filename": "review.txt",
                        "excerpt": "Версия 2.1 от 01.09.2025",
                    },
                    {
                        "index": 2,
                        "document_id": str(uuid.uuid4()),
                        "filename": "other.txt",
                        "excerpt": "Нецитируемый текст 3.1",
                    },
                ],
            },
        )

    async def run_case():
        async with httpx.AsyncClient(
            base_url="http://test/api/v1",
            transport=httpx.MockTransport(handler),
        ) as client:
            return await evaluate_case(
                client,
                workspace_id,
                EvaluationCase(
                    id="exact-001",
                    question="Какая версия?",
                    expected_filenames=("review.txt",),
                    question_type="exact_value",
                    expected_answer="2.1",
                    required_facts=("2.1", "01.09.2025"),
                    forbidden_facts=("3.1",),
                ),
                limit=10,
                mode="hybrid",
                rerank=False,
                include_answers=True,
                answer_temperature=0.0,
            )

    result = asyncio.run(run_case())

    assert result["answer_metrics"]["required_fact_coverage"] == 1.0
    assert result["answer_metrics"]["context_fact_coverage"] == 1.0
    assert result["answer_metrics"]["context_source_coverage"] == 1.0
    assert result["answer_metrics"]["gold_context_fact_coverage"] == 1.0
    assert result["answer_metrics"]["citation_fact_coverage"] == 1.0
    assert result["answer_metrics"]["citation_source_coverage"] == 1.0
    assert result["answer_metrics"]["forbidden_fact_violation"] is False
    assert result["answer"]["abstention_reason"] is None
    assert result["answer"]["cited_source_indices"] == [1]
    assert result["answer"]["generation_temperature"] == 0.0
