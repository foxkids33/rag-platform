from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from functools import lru_cache

import numpy as np
from fastapi import FastAPI, HTTPException
from fastembed import TextEmbedding
from typing import Literal

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("rag-embedding")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_native_dim: int = 384
    embedding_dim: int = 1024
    embedding_cache_dir: str = "/models"


settings = Settings()


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


@lru_cache
def get_model() -> TextEmbedding:
    logger.info("Loading embedding model %s", settings.embedding_model)
    model = TextEmbedding(
        model_name=settings.embedding_model,
        cache_dir=settings.embedding_cache_dir,
    )
    logger.info("Embedding model is ready")
    return model


def resize_vector(vector: np.ndarray, output_dimension: int, normalize: bool = True) -> list[float]:
    values = np.asarray(vector, dtype=np.float32).reshape(-1)
    if values.size > output_dimension:
        raise ValueError(
            f"Embedding dimension {values.size} exceeds configured output dimension {output_dimension}"
        )

    if values.size < output_dimension:
        values = np.pad(values, (0, output_dimension - values.size))

    if normalize:
        norm = float(np.linalg.norm(values))
        if norm > 0:
            values = values / norm

    return values.astype(float).tolist()


def _embed(
    inputs: list[str],
    normalize: bool,
    input_type: Literal["query", "passage"],
) -> list[list[float]]:
    model = get_model()
    if input_type == "query":
        vectors = list(model.query_embed(inputs))
    else:
        vectors = list(model.passage_embed(inputs))
    if len(vectors) != len(inputs):
        raise RuntimeError("Embedding model returned an unexpected number of vectors")

    result: list[list[float]] = []
    for vector in vectors:
        if len(vector) != settings.embedding_native_dim:
            logger.warning(
                "Model produced dimension %d; configured native dimension is %d",
                len(vector),
                settings.embedding_native_dim,
            )
        result.append(resize_vector(vector, settings.embedding_dim, normalize=normalize))
    return result


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(get_model)
    yield


app = FastAPI(title="RAG Embedding Service", version="0.1.0", lifespan=lifespan)


@app.get("/health/ready", response_model=HealthOut)
async def health_ready() -> HealthOut:
    get_model()
    return HealthOut(
        status="ready",
        model=settings.embedding_model,
        native_dimension=settings.embedding_native_dim,
        output_dimension=settings.embedding_dim,
    )


@app.post("/embed", response_model=list[list[float]])
async def embed(payload: EmbedRequest) -> list[list[float]]:
    inputs = [payload.inputs] if isinstance(payload.inputs, str) else payload.inputs
    cleaned = [item.strip() for item in inputs]
    if not cleaned or any(not item for item in cleaned):
        raise HTTPException(status_code=400, detail="Embedding inputs must not be empty")
    if len(cleaned) > 64:
        raise HTTPException(status_code=400, detail="A maximum of 64 inputs is allowed per request")

    try:
        return await asyncio.to_thread(
            _embed, cleaned, payload.normalize, payload.input_type
        )
    except Exception as exc:
        logger.exception("Embedding request failed")
        raise HTTPException(status_code=500, detail="Embedding generation failed") from exc
