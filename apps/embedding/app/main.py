from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Literal

import numpy as np
from fastapi import FastAPI, HTTPException
from fastembed import TextEmbedding
from fastembed.common.model_description import ModelSource, PoolingType

from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("rag-embedding")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_native_dim: int = 384
    embedding_dim: int = 384
    embedding_cache_dir: str = "/models"
    embedding_context_tokens: int = 512
    embedding_query_prefix: str | None = None
    embedding_passage_prefix: str | None = None
    embedding_max_concurrency: int = 2


settings = Settings()
inference_slots = asyncio.Semaphore(max(1, settings.embedding_max_concurrency))


CUSTOM_MODELS = {
    "intfloat/multilingual-e5-small": {
        "pooling": PoolingType.MEAN,
        "normalization": True,
        "sources": ModelSource(hf="intfloat/multilingual-e5-small"),
        "dim": 384,
        "model_file": "onnx/model.onnx",
        "additional_files": [
            "tokenizer.json",
            "tokenizer_config.json",
            "special_tokens_map.json",
            "sentencepiece.bpe.model",
        ],
        "description": "Multilingual E5 small (94 languages, 512-token context)",
        "license": "mit",
        "size_in_gb": 0.5,
    },
}


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


def _default_prefix(model_name: str, input_type: Literal["query", "passage"]) -> str:
    if "multilingual-e5" in model_name.lower():
        return "query: " if input_type == "query" else "passage: "
    return ""


def prepare_inputs(
    inputs: list[str],
    input_type: Literal["query", "passage"],
    model_name: str,
    query_prefix: str | None = None,
    passage_prefix: str | None = None,
) -> list[str]:
    configured = query_prefix if input_type == "query" else passage_prefix
    prefix = configured if configured is not None else _default_prefix(model_name, input_type)
    if not prefix:
        return inputs
    return [item if item.lower().startswith(prefix.lower()) else f"{prefix}{item}" for item in inputs]


def register_custom_model(model_name: str) -> None:
    if any(
        model["model"].lower() == model_name.lower()
        for model in TextEmbedding.list_supported_models()
    ):
        return
    specification = CUSTOM_MODELS.get(model_name.lower())
    if specification is None:
        return
    TextEmbedding.add_custom_model(model=model_name, **specification)


@lru_cache
def get_model() -> TextEmbedding:
    logger.info("Loading embedding model %s", settings.embedding_model)
    register_custom_model(settings.embedding_model)
    model = TextEmbedding(
        model_name=settings.embedding_model,
        cache_dir=settings.embedding_cache_dir,
    )
    logger.info("Embedding model is ready")
    return model


def prepare_vector(vector: np.ndarray, output_dimension: int, normalize: bool = True) -> list[float]:
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


def _embed(
    inputs: list[str],
    normalize: bool,
    input_type: Literal["query", "passage"],
) -> list[list[float]]:
    model = get_model()
    prepared = prepare_inputs(
        inputs,
        input_type,
        settings.embedding_model,
        settings.embedding_query_prefix,
        settings.embedding_passage_prefix,
    )
    vectors = list(model.embed(prepared))
    if len(vectors) != len(inputs):
        raise RuntimeError("Embedding model returned an unexpected number of vectors")

    result: list[list[float]] = []
    for vector in vectors:
        if len(vector) != settings.embedding_native_dim:
            raise RuntimeError(
                f"Model produced dimension {len(vector)}; configured native dimension is "
                f"{settings.embedding_native_dim}"
            )
        result.append(prepare_vector(vector, settings.embedding_dim, normalize=normalize))
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
        context_tokens=settings.embedding_context_tokens,
        input_prefixes=bool(
            _default_prefix(settings.embedding_model, "query")
            or settings.embedding_query_prefix
            or settings.embedding_passage_prefix
        ),
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
        raise HTTPException(
            status_code=400,
            detail="truncate=false is not supported by the configured ONNX embedding model",
        )

    try:
        async with inference_slots:
            return await asyncio.to_thread(
                _embed, cleaned, payload.normalize, payload.input_type
            )
    except Exception as exc:
        logger.exception("Embedding request failed")
        raise HTTPException(status_code=500, detail="Embedding generation failed") from exc
