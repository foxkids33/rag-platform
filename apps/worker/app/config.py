from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://rag:rag@localhost:5432/rag"
    redis_url: str = "redis://localhost:6379/0"
    ingestion_queue: str = "rag:ingestion"
    ingestion_poll_seconds: float = Field(default=1.0, gt=0, le=30)
    ingestion_heartbeat_seconds: float = Field(default=15.0, gt=0, le=300)
    ingestion_stale_after_seconds: int = Field(default=300, ge=5, le=86_400)
    ingestion_recovery_interval_seconds: float = Field(default=30.0, gt=0, le=3600)
    ingestion_max_attempts: int = Field(default=3, ge=1, le=20)
    object_deletion_max_attempts: int = Field(default=10, ge=1, le=100)

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
    embedding_dim: int = 384
    embedding_timeout_seconds: float = 120.0
    embedding_batch_size: int = 32

    @model_validator(mode="after")
    def validate_job_lease_timing(self) -> "Settings":
        if self.ingestion_stale_after_seconds < 3 * self.ingestion_heartbeat_seconds:
            raise ValueError(
                "INGESTION_STALE_AFTER_SECONDS must be at least three times "
                "INGESTION_HEARTBEAT_SECONDS"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
