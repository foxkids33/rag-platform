from functools import lru_cache

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

OIDC_ASYMMETRIC_ALGORITHMS = {
    "RS256",
    "RS384",
    "RS512",
    "PS256",
    "PS384",
    "PS512",
    "ES256",
    "ES384",
    "ES512",
    "EdDSA",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "local"
    app_name: str = "RAG Platform"
    api_host: str = "0.0.0.0"
    api_port: int = 8000
    web_origin: str = "http://localhost:5173"

    auth_enabled: bool = False
    auth_local_subject: str = "local-user"
    auth_local_tenant: str = "local"
    auth_admin_role: str = "rag-admin"
    oidc_issuer: str = ""
    oidc_audience: str = ""
    oidc_jwks_url: str = ""
    oidc_algorithm: str = "RS256"
    oidc_tenant_claim: str = "tenant_id"
    oidc_roles_claim: str = "roles"
    oidc_jwks_cache_seconds: int = Field(default=300, gt=0, le=86_400)
    oidc_jwks_timeout_seconds: float = Field(default=5.0, gt=0, le=30)
    oidc_token_leeway_seconds: int = Field(default=30, ge=0, le=300)

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
    rag_min_source_score: float = 0.05
    rag_relative_source_score: float = 0.25
    rag_source_similarity_threshold: float = 0.78
    rag_max_sources_per_document: int = 3
    rag_query_selection_weight: float = Field(default=0.30, ge=0.0, le=1.0)
    rag_strong_evidence_score: float = 0.50
    rag_limited_evidence_score: float = 0.15
    embedding_base_url: str = "http://embedding:8080"
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dim: int = 384
    embedding_timeout_seconds: float = 120.0
    rerank_base_url: str = "http://reranker:8081"
    rerank_model: str = "cross-encoder/mmarco-mMiniLMv2-L12-H384-v1"
    rerank_enabled: bool = True
    rerank_timeout_seconds: float = 180.0
    rerank_candidate_limit: int = 20
    rerank_document_chars: int = 1800
    rerank_rank_weight: float = 0.7
    rerank_retrieval_weight: float = 0.3

    @model_validator(mode="after")
    def validate_authentication(self) -> "Settings":
        if self.auth_enabled:
            required = {
                "OIDC_ISSUER": self.oidc_issuer,
                "OIDC_AUDIENCE": self.oidc_audience,
                "OIDC_JWKS_URL": self.oidc_jwks_url,
            }
            missing = [name for name, value in required.items() if not value.strip()]
            if missing:
                raise ValueError(
                    "Authentication is enabled but required settings are missing: "
                    + ", ".join(missing)
                )
        required_names = {
            "AUTH_LOCAL_SUBJECT": self.auth_local_subject,
            "AUTH_LOCAL_TENANT": self.auth_local_tenant,
            "AUTH_ADMIN_ROLE": self.auth_admin_role,
            "OIDC_TENANT_CLAIM": self.oidc_tenant_claim,
            "OIDC_ROLES_CLAIM": self.oidc_roles_claim,
        }
        empty = [name for name, value in required_names.items() if not value.strip()]
        if empty:
            raise ValueError("Authentication settings must not be empty: " + ", ".join(empty))
        if self.oidc_algorithm not in OIDC_ASYMMETRIC_ALGORITHMS:
            raise ValueError("OIDC_ALGORITHM must be an approved asymmetric algorithm")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
