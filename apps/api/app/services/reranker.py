from __future__ import annotations

from dataclasses import dataclass

import httpx

from app.core.config import settings


class RerankError(RuntimeError):
    pass


@dataclass(frozen=True)
class RerankOutput:
    model: str
    scores: list[float]


class RerankerClient:
    async def rerank(self, query: str, documents: list[str]) -> RerankOutput:
        if not documents:
            return RerankOutput(model=settings.rerank_model, scores=[])

        url = f"{settings.rerank_base_url.rstrip('/')}/rerank"
        timeout = httpx.Timeout(settings.rerank_timeout_seconds)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    url,
                    json={
                        "query": query,
                        "documents": documents,
                        "normalize": True,
                    },
                )
                response.raise_for_status()
                payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RerankError(f"Reranker service is unavailable: {exc}") from exc

        scores = payload.get("scores")
        if not isinstance(scores, list) or len(scores) != len(documents):
            raise RerankError("Reranker returned an unexpected number of scores")

        try:
            parsed_scores = [float(score) for score in scores]
        except (TypeError, ValueError) as exc:
            raise RerankError("Reranker returned invalid scores") from exc

        return RerankOutput(
            model=str(payload.get("model") or settings.rerank_model),
            scores=parsed_scores,
        )


reranker = RerankerClient()
