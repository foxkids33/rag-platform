from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.core.config import settings
from app.services.embeddings import embeddings
from app.services.queue import ingestion_queue


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await embeddings.close()
    await ingestion_queue.close()


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.web_origin],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router)


@app.get("/")
async def root() -> dict:
    return {"service": settings.app_name, "docs": "/docs", "health": "/api/v1/health"}
