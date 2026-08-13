from __future__ import annotations

import asyncio
import uuid

import httpx

from app.evaluation.models import EvaluationCase
from app.evaluation.runner import evaluate_case


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
            )

    result = asyncio.run(run_case())

    assert result["gold_key"] == "filename"
    assert result["metrics"]["recall_at_1"] == 1.0
    assert result["metrics"]["recall_at_5"] == 1.0
