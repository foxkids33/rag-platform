from functools import lru_cache
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
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

    # CPU parsers do not initialize Docling, Torch or download any model weights.
    parser_max_input_mb: int = Field(default=100, ge=1, le=200)
    parser_max_text_chars: int = Field(default=2_000_000, ge=1000, le=10_000_000)
    parser_timeout_seconds: float = Field(default=240.0, ge=5, le=600)
    parser_memory_mb: int = Field(default=1536, ge=384, le=4096)
    parser_cpu_seconds: int = Field(default=120, ge=5, le=300)
    pdf_max_pages: int = Field(default=0, ge=0)
    pdf_timeout_seconds: float = Field(default=3600, ge=5, le=86_400)
    image_max_pixels: int = Field(default=16_000_000, ge=10_000, le=40_000_000)
    image_max_frames: int = Field(default=10, ge=1, le=30)
    ocr_enabled: bool = True
    ocr_languages: str = "rus+eng"
    ocr_timeout_seconds: float = Field(default=20.0, gt=0, le=60)
    ocr_total_timeout_seconds: float = Field(default=120.0, gt=0, le=240)
    ocr_max_pages: int = Field(default=10, ge=1, le=30)
    ocr_render_max_edge: int = Field(default=2800, ge=640, le=4096)
    vision_enabled: bool = False
    vision_base_url: str = ""
    vision_model: str = ""
    vision_api_key: SecretStr = SecretStr("")
    vision_allow_insecure_http: bool = False
    vision_timeout_seconds: float = Field(default=30.0, gt=0, le=60)
    vision_max_images_per_document: int = Field(default=4, ge=1, le=10)
    vision_max_image_edge: int = Field(default=1600, ge=256, le=2048)
    vision_max_tokens: int = Field(default=900, ge=64, le=2000)
    vision_enable_thinking: bool | None = None

    @property
    def pdf_batch_pages(self) -> int:
        return min(10, self.ocr_max_pages)

    @field_validator("ocr_languages")
    @classmethod
    def validate_ocr_languages(cls, value: str) -> str:
        import re

        if not re.fullmatch(r"[A-Za-z_]{2,24}(?:\+[A-Za-z_]{2,24}){0,4}", value):
            raise ValueError("OCR_LANGUAGES must contain explicit Tesseract language codes")
        return value

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
        if self.vision_enabled:
            parsed = urlsplit(self.vision_base_url.strip())
            if (
                not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment
                or parsed.scheme not in {"http", "https"}
            ):
                raise ValueError("VISION_BASE_URL must be an absolute URL without credentials")
            if parsed.scheme == "http" and not self.vision_allow_insecure_http:
                raise ValueError("HTTP vision requires explicit VISION_ALLOW_INSECURE_HTTP")
            if not self.vision_model.strip():
                raise ValueError("VISION_MODEL is required when vision is enabled")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
