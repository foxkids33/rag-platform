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
    ingestion_queue: str = "rag:ingestion"

    minio_endpoint: str = "localhost:9000"
    minio_access_key: str = "minio"
    minio_secret_key: str = "minio123"
    minio_bucket: str = "rag-documents"
    minio_secure: bool = False
    upload_max_mb: int = 100

    @property
    def upload_max_bytes(self) -> int:
        return self.upload_max_mb * 1024 * 1024

    vllm_base_url: str = "http://vllm-host:8000"
    vllm_api_key: str = ""
    vllm_model: str = ""
    llm_timeout_seconds: float = 180.0
    llm_max_tokens: int = 800
    llm_temperature: float = 0.1
    rag_context_max_chars: int = 12000
    rag_source_max_chars: int = 2200
    rag_source_limit: int = 5
    rag_history_messages: int = 8
    rag_history_max_chars: int = 8000
    rag_rewrite_followups: bool = True
    rag_rewrite_max_tokens: int = 160
    embedding_base_url: str = "http://embedding:8080"
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dim: int = 1024
    embedding_timeout_seconds: float = 120.0
    rerank_base_url: str = "http://reranker:8081"
    rerank_model: str = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    rerank_enabled: bool = True
    rerank_timeout_seconds: float = 180.0
    rerank_candidate_limit: int = 20
    rerank_document_chars: int = 1800
    rerank_rank_weight: float = 0.7
    rerank_retrieval_weight: float = 0.3


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
