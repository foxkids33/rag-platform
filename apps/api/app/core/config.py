from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    app_name: str = "RAG Platform"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    web_origin: str = "http://localhost:5173"

    database_url: str = "postgresql+asyncpg://rag:rag@localhost:5432/rag"
    redis_url: str = "redis://localhost:6379/0"

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minio"
    minio_secret_key: str = "minio123"
    minio_bucket: str = "rag-documents"
    minio_secure: bool = False

    vllm_base_url: str = "http://localhost:8001"
    vllm_api_key: str = ""
    vllm_model: str = ""
    embedding_base_url: str = "http://localhost:8080"
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = 1024
    rerank_base_url: str = "http://localhost:8081"
    rerank_model: str = "BAAI/bge-reranker-v2-m3"


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
