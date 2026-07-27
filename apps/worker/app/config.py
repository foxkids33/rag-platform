from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://rag:rag@localhost:5432/rag"
    redis_url: str = "redis://localhost:6379/0"
    ingestion_queue: str = "rag:ingestion"

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minio"
    minio_secret_key: str = "minio123"
    minio_bucket: str = "rag-documents"
    minio_secure: bool = False

    chunk_max_chars: int = 2200
    chunk_overlap_chars: int = 300
    chunk_min_chars: int = 180
    parent_max_chars: int = 12000

    docling_chunk_max_tokens: int = 450
    chunk_tokenizer_model: str = "BAAI/bge-m3"

    embedding_base_url: str = "http://embedding:8080"
    embedding_dim: int = 1024
    embedding_timeout_seconds: float = 120.0
    embedding_batch_size: int = 32


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
