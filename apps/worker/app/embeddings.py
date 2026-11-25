from __future__ import annotations

import httpx

from app.config import settings


class EmbeddingError(RuntimeError):
    pass


class EmbeddingClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=settings.embedding_base_url.rstrip("/"),
            timeout=settings.embedding_timeout_seconds,
        )

    async def embed(self, inputs: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(inputs), settings.embedding_batch_size):
            batch = inputs[start : start + settings.embedding_batch_size]
            try:
                response = await self._client.post(
                    "/embed",
                    json={"inputs": batch, "normalize": True, "truncate": True, "input_type": "passage"},
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                raise EmbeddingError("Embedding service is unavailable") from exc

            payload = response.json()
            if not isinstance(payload, list) or len(payload) != len(batch):
                raise EmbeddingError("Embedding service returned an invalid response")

            for vector in payload:
                if not isinstance(vector, list) or len(vector) != settings.embedding_dim:
                    raise EmbeddingError(
                        f"Expected embedding dimension {settings.embedding_dim}"
                    )
                vectors.append([float(value) for value in vector])

        return vectors

    async def close(self) -> None:
        await self._client.aclose()


embeddings = EmbeddingClient()
