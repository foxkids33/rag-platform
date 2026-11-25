from __future__ import annotations

import httpx

from app.core.config import settings


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.embedding_base_url.rstrip("/"),
            timeout=settings.embedding_timeout_seconds,
        )

    async def embed(self, inputs: list[str]) -> list[list[float]]:
        if not inputs:
            return []

        try:
            response = await self._client.post(
                "/embed",
                json={"inputs": inputs, "normalize": True, "truncate": True, "input_type": "query"},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise EmbeddingError("Embedding service is unavailable") from exc

        payload = response.json()
        if not isinstance(payload, list) or len(payload) != len(inputs):
            raise EmbeddingError("Embedding service returned an invalid response")

        vectors: list[list[float]] = []
        for vector in payload:
            if not isinstance(vector, list) or len(vector) != settings.embedding_dim:
                raise EmbeddingError(
                    f"Expected embedding dimension {settings.embedding_dim}"
                )
            vectors.append([float(value) for value in vector])
        return vectors

    async def embed_query(self, query: str) -> list[float]:
        return (await self.embed([query]))[0]

    async def close(self) -> None:
        await self._client.aclose()


embeddings = EmbeddingClient()
