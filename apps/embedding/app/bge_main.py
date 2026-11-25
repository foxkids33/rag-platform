from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Any, Literal

import numpy as np
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("rag-embedding-bge")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    embedding_model: str = "BAAI/bge-m3"
    embedding_native_dim: int = 1024
    embedding_dim: int = 1024
    embedding_context_tokens: int = 1024
    embedding_batch_size: int = 4
    embedding_max_concurrency: int = 1
    embedding_device: str = "cuda:0"
    embedding_use_fp16: bool = True


settings = Settings()
inference_slots = asyncio.Semaphore(max(1, settings.embedding_max_concurrency))


class EmbedRequest(BaseModel):
    inputs: str | list[str]
    normalize: bool = True
    truncate: bool = True
    input_type: Literal["query", "passage"] = "passage"


class HealthOut(BaseModel):
    status: str
    model: str
    native_dimension: int
    output_dimension: int
    context_tokens: int
    input_prefixes: bool
    device: str
    precision: str
    backend: str


@lru_cache
def get_model() -> Any:
    import torch
    from FlagEmbedding import BGEM3FlagModel

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable for the BGE-M3 GPU backend")
    logger.info(
        "Loading %s on %s (%s)",
        settings.embedding_model,
        settings.embedding_device,
        "fp16" if settings.embedding_use_fp16 else "fp32",
    )
    model = BGEM3FlagModel(
        settings.embedding_model,
        use_fp16=settings.embedding_use_fp16,
        devices=[settings.embedding_device],
    )
    logger.info("BGE-M3 embedding model is ready")
    return model


def prepare_vector(vector: Any, output_dimension: int, *, normalize: bool) -> list[float]:
    values = np.asarray(vector, dtype=np.float32).reshape(-1)
    if values.size != output_dimension:
        raise ValueError(
            f"Embedding dimension {values.size} does not match configured output dimension "
            f"{output_dimension}"
        )
    if normalize:
        norm = float(np.linalg.norm(values))
        if norm > 0:
            values = values / norm
    return values.astype(float).tolist()


def _embed(inputs: list[str], normalize: bool) -> list[list[float]]:
    encoded = get_model().encode(
        inputs,
        batch_size=settings.embedding_batch_size,
        max_length=settings.embedding_context_tokens,
        return_dense=True,
        return_sparse=False,
        return_colbert_vecs=False,
    )
    vectors = encoded.get("dense_vecs") if isinstance(encoded, dict) else None
    if vectors is None or len(vectors) != len(inputs):
        raise RuntimeError("BGE-M3 returned an unexpected dense embedding result")
    return [
        prepare_vector(vector, settings.embedding_dim, normalize=normalize)
        for vector in vectors
    ]


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(get_model)
    yield


app = FastAPI(title="RAG BGE-M3 GPU Embedding Service", version="0.1.0", lifespan=lifespan)


@app.get("/health/ready", response_model=HealthOut)
async def health_ready() -> HealthOut:
    get_model()
    return HealthOut(
        status="ready",
        model=settings.embedding_model,
        native_dimension=settings.embedding_native_dim,
        output_dimension=settings.embedding_dim,
        context_tokens=settings.embedding_context_tokens,
        input_prefixes=False,
        device=settings.embedding_device,
        precision="fp16" if settings.embedding_use_fp16 else "fp32",
        backend="FlagEmbedding",
    )


@app.post("/embed", response_model=list[list[float]])
async def embed(payload: EmbedRequest) -> list[list[float]]:
    inputs = [payload.inputs] if isinstance(payload.inputs, str) else payload.inputs
    cleaned = [item.strip() for item in inputs]
    if not cleaned or any(not item for item in cleaned):
        raise HTTPException(status_code=400, detail="Embedding inputs must not be empty")
    if len(cleaned) > 64:
        raise HTTPException(status_code=400, detail="A maximum of 64 inputs is allowed per request")
    if not payload.truncate:
        raise HTTPException(status_code=400, detail="truncate=false is not supported")
    try:
        async with inference_slots:
            return await asyncio.to_thread(_embed, cleaned, payload.normalize)
    except Exception as exc:
        logger.exception("BGE-M3 embedding request failed")
        raise HTTPException(status_code=500, detail="Embedding generation failed") from exc
