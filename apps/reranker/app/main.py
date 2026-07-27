from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import lru_cache

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from transformers import AutoModelForSequenceClassification, AutoTokenizer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("rag-reranker")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    rerank_model: str = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    rerank_cache_dir: str = "/models"
    rerank_max_length: int = 512
    rerank_batch_size: int = 8
    rerank_threads: int = 4
    rerank_max_documents: int = 50
    rerank_max_document_chars: int = 1800


settings = Settings()


class RerankRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    documents: list[str] = Field(min_length=1, max_length=50)
    normalize: bool = True


class RerankResponse(BaseModel):
    model: str
    normalized: bool
    scores: list[float]


class HealthOut(BaseModel):
    status: str
    model: str


@dataclass(frozen=True)
class ModelBundle:
    tokenizer: object
    model: AutoModelForSequenceClassification


@lru_cache
def get_model() -> ModelBundle:
    torch.set_num_threads(max(1, settings.rerank_threads))
    logger.info("Loading reranker model %s", settings.rerank_model)
    tokenizer = AutoTokenizer.from_pretrained(
        settings.rerank_model,
        cache_dir=settings.rerank_cache_dir,
    )
    model = AutoModelForSequenceClassification.from_pretrained(
        settings.rerank_model,
        cache_dir=settings.rerank_cache_dir,
    )
    model.eval()
    logger.info("Reranker model is ready")
    return ModelBundle(tokenizer=tokenizer, model=model)


def _logits_to_scores(logits: torch.Tensor) -> torch.Tensor:
    if logits.ndim == 1:
        return logits
    if logits.ndim == 2 and logits.shape[1] == 1:
        return logits[:, 0]
    if logits.ndim == 2 and logits.shape[1] == 2:
        return logits[:, 1] - logits[:, 0]
    raise RuntimeError(f"Unexpected reranker logits shape: {tuple(logits.shape)}")


def _rerank(query: str, documents: list[str], normalize: bool) -> list[float]:
    bundle = get_model()
    scores: list[float] = []

    for start in range(0, len(documents), settings.rerank_batch_size):
        batch = documents[start : start + settings.rerank_batch_size]
        pairs = [[query, document] for document in batch]
        encoded = bundle.tokenizer(
            pairs,
            padding=True,
            truncation=True,
            max_length=settings.rerank_max_length,
            return_tensors="pt",
        )
        with torch.inference_mode():
            logits = bundle.model(**encoded).logits
        batch_scores = _logits_to_scores(logits).float().cpu()
        if normalize:
            batch_scores = torch.sigmoid(batch_scores)
        scores.extend(float(value) for value in batch_scores.tolist())

    return scores


@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(get_model)
    yield


app = FastAPI(title="RAG Reranker Service", version="0.1.0", lifespan=lifespan)


@app.get("/health/live", response_model=HealthOut)
async def health_live() -> HealthOut:
    return HealthOut(status="live", model=settings.rerank_model)


@app.get("/health/ready", response_model=HealthOut)
async def health_ready() -> HealthOut:
    get_model()
    return HealthOut(status="ready", model=settings.rerank_model)


@app.post("/rerank", response_model=RerankResponse)
async def rerank(payload: RerankRequest) -> RerankResponse:
    query = payload.query.strip()
    documents = [document.strip() for document in payload.documents]
    if not query or any(not document for document in documents):
        raise HTTPException(status_code=400, detail="Query and documents must not be empty")
    if len(documents) > settings.rerank_max_documents:
        raise HTTPException(
            status_code=400,
            detail=f"A maximum of {settings.rerank_max_documents} documents is allowed",
        )

    trimmed = [document[: settings.rerank_max_document_chars] for document in documents]
    try:
        scores = await asyncio.to_thread(_rerank, query, trimmed, payload.normalize)
    except Exception as exc:
        logger.exception("Reranking request failed")
        raise HTTPException(status_code=500, detail="Reranking failed") from exc

    return RerankResponse(
        model=settings.rerank_model,
        normalized=payload.normalize,
        scores=scores,
    )
