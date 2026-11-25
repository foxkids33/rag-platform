import re
from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
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
    auth_mode: Literal["oidc", "password"] = "oidc"
    auth_registration_enabled: bool = True
    auth_cookie_secure: bool = True
    auth_session_seconds: int = Field(default=28_800, ge=300, le=604_800)
    auth_remember_session_seconds: int = Field(default=604_800, ge=300, le=2_592_000)
    auth_max_registered_users: int = Field(default=100, ge=1, le=10_000)
    user_upload_max_mb: int = Field(default=25, ge=1, le=100)
    user_image_max_mb: int = Field(default=10, ge=1, le=100)
    user_storage_max_mb: int = Field(default=512, ge=1, le=102_400)
    password_uploads_max_mb: int = Field(default=2048, ge=1, le=1_048_576)
    user_max_workspaces: int = Field(default=10, ge=1, le=1000)
    user_max_documents_per_workspace: int = Field(default=100, ge=1, le=10_000)
    auth_allow_unauthenticated_pilot: bool = False
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

    confluence_enabled: bool = False
    confluence_deployment: Literal["cloud", "data_center"] = "cloud"
    confluence_base_url: str = ""
    confluence_auth_mode: Literal["basic", "bearer"] = "basic"
    confluence_email: str = ""
    confluence_api_token: SecretStr = SecretStr("")
    confluence_access_token: SecretStr = SecretStr("")
    confluence_timeout_seconds: float = Field(default=30.0, gt=0, le=180)
    confluence_verify_ssl: bool = True
    confluence_allow_insecure_http: bool = False
    confluence_max_pages_per_import: int = Field(default=500, ge=1, le=500)

    # Explicit profiles choose direct or connector. Auto preserves older deployments.
    confluence_connection_mode: Literal["auto", "direct", "connector"] = "auto"
    confluence_allowed_spaces: str = ""

    @field_validator("confluence_allowed_spaces")
    @classmethod
    def validate_confluence_allowed_spaces(cls, value: str) -> str:
        keys = [key.strip() for key in value.split(",") if key.strip()]
        pattern = r"(?:[A-Za-z0-9_-]{1,255}|~[A-Za-z0-9_][A-Za-z0-9_.@-]{0,253})"
        if len(keys) > 1000 or any(not re.fullmatch(pattern, key) for key in keys):
            raise ValueError("CONFLUENCE_ALLOWED_SPACES must contain explicit comma-separated space keys")
        return ",".join(dict.fromkeys(keys))

    @property
    def confluence_allowed_space_keys(self) -> list[str]:
        return [key.strip() for key in self.confluence_allowed_spaces.split(",") if key.strip()]

    confluence_connector_enabled: bool = True
    confluence_connector_enrollment_minutes: int = Field(default=15, ge=1, le=1440)
    confluence_connector_offline_seconds: int = Field(default=75, ge=15, le=3600)
    confluence_connector_lease_seconds: int = Field(default=180, ge=30, le=3600)
    confluence_connector_job_timeout_seconds: int = Field(default=300, ge=30, le=3600)
    confluence_connector_max_job_seconds: int = Field(default=1800, ge=300, le=3600)
    confluence_connector_max_attempts: int = Field(default=3, ge=1, le=10)
    confluence_connector_long_poll_seconds: int = Field(default=25, ge=1, le=30)
    confluence_connector_max_pages_per_job: int = Field(default=50, ge=1, le=100)

    @property
    def upload_max_bytes(self) -> int:
        return self.upload_max_mb * 1024 * 1024

    vllm_base_url: str = "http://vllm-host:8000"
    vllm_api_key: str = ""
    vllm_model: str = ""
    llm_timeout_seconds: float = 180.0
    llm_max_tokens: int = 800
    llm_temperature: float = 0.1
    llm_enable_thinking: bool | None = None
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
    rag_answer_candidate_limit: int = Field(default=20, ge=1, le=20)
    rag_strong_evidence_score: float = 0.50
    rag_limited_evidence_score: float = 0.15
    embedding_base_url: str = "http://embedding:8080"
    embedding_model: str = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    embedding_dim: int = 384
    embedding_schema_initialization_allowed: bool = False
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
        protected_environment = self.app_env.casefold() in {
            "server",
            "production",
            "prod",
        }
        pilot_exception = (
            self.app_env.casefold() == "server" and self.auth_allow_unauthenticated_pilot
        )
        if protected_environment and not self.auth_enabled and not pilot_exception:
            raise ValueError("Authentication must be enabled outside local development")
        if self.auth_enabled and self.auth_mode == "oidc":
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
        if (
            self.auth_enabled and self.auth_mode == "password" and protected_environment
            and (not self.auth_cookie_secure or urlsplit(self.web_origin).scheme != "https")
        ):
            raise ValueError(
                "Password authentication requires HTTPS WEB_ORIGIN and secure cookies "
                "outside local development"
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
        if self.confluence_enabled:
            parsed = urlsplit(self.confluence_base_url.strip())
            if not parsed.scheme or not parsed.netloc or parsed.username or parsed.password:
                raise ValueError("CONFLUENCE_BASE_URL must be an absolute URL without credentials")
            if parsed.query or parsed.fragment:
                raise ValueError("CONFLUENCE_BASE_URL must not contain a query or fragment")
            if parsed.scheme != "https" and not (
                parsed.scheme == "http" and self.confluence_allow_insecure_http
            ):
                raise ValueError(
                    "CONFLUENCE_BASE_URL must use HTTPS; explicitly set "
                    "CONFLUENCE_ALLOW_INSECURE_HTTP=true for an internal HTTP deployment"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
