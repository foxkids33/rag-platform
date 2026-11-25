from __future__ import annotations

from functools import lru_cache
from hashlib import sha256
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_CREDENTIAL_DIRECTORY = Path("~/.config/rag-confluence-connector")


def normalize_rag_url(value: str) -> str:
    """Normalize destination identity without merging different API base paths."""
    parsed = urlsplit(value.strip())
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port == 0
    ):
        raise ValueError("RAG destination must be an absolute HTTP(S) URL without credentials")
    hostname = (parsed.hostname or "").lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    port = parsed.port
    if port is not None and (scheme, port) not in {("http", 80), ("https", 443)}:
        hostname = f"{hostname}:{port}"
    return urlunsplit((scheme, hostname, parsed.path.rstrip("/"), "", ""))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env.connector", extra="ignore")

    rag_url: str
    rag_enrollment_token: SecretStr = SecretStr("")
    # None means a separate private file for each destination. An explicitly configured
    # path remains strictly bound to its saved RAG_URL, even if it is the old default.
    rag_credential_file: Path | None = None
    rag_allow_insecure_http: bool = False
    rag_verify_ssl: bool = True
    rag_ca_file: Path | None = None
    rag_poll_seconds: int = Field(default=25, ge=1, le=30)
    rag_request_timeout_seconds: float = Field(default=45.0, gt=5, le=300)

    confluence_url: str
    confluence_auth_mode: Literal["basic", "bearer", "cookie"] = "bearer"
    confluence_username: str = ""
    confluence_token: SecretStr = SecretStr("")
    confluence_token_file: Path | None = None
    confluence_cookie_file: Path | None = None
    confluence_allowed_spaces: str = "KBR"
    confluence_verify_ssl: bool = True
    confluence_ca_file: Path | None = None
    confluence_timeout_seconds: float = Field(default=30.0, gt=1, le=180)
    confluence_request_attempts: int = Field(default=3, ge=1, le=5)
    confluence_retry_delay_seconds: float = Field(default=1.0, ge=0, le=10)
    confluence_max_results: int = Field(default=1000, ge=1, le=5000)

    @property
    def allowed_spaces(self) -> tuple[str, ...]:
        values = [item.strip() for item in self.confluence_allowed_spaces.split(",")]
        return tuple(dict.fromkeys(item for item in values if item))

    @property
    def credential_path(self) -> Path:
        if self.rag_credential_file is not None:
            return self.rag_credential_file.expanduser()
        destination = sha256(normalize_rag_url(self.rag_url).encode("utf-8")).hexdigest()
        return DEFAULT_CREDENTIAL_DIRECTORY.expanduser() / f"credential-{destination}.json"

    @property
    def legacy_credential_path(self) -> Path:
        return DEFAULT_CREDENTIAL_DIRECTORY.expanduser() / "credential.json"

    @staticmethod
    def _validate_url(name: str, value: str, allow_http: bool = False) -> None:
        parsed = urlsplit(value.strip())
        if not parsed.scheme or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError(f"{name} must be an absolute URL without credentials")
        if parsed.query or parsed.fragment:
            raise ValueError(f"{name} must not contain a query or fragment")
        # Accessing port validates malformed or out-of-range port declarations.
        if not parsed.hostname or parsed.port == 0:
            raise ValueError(f"{name} must contain a valid hostname and port")
        if parsed.scheme != "https" and not (allow_http and parsed.scheme == "http"):
            raise ValueError(f"{name} must use HTTPS")

    @model_validator(mode="after")
    def validate_connector(self) -> Settings:
        self._validate_url("RAG_URL", self.rag_url, self.rag_allow_insecure_http)
        self._validate_url("CONFLUENCE_URL", self.confluence_url)
        if not self.allowed_spaces:
            raise ValueError("CONFLUENCE_ALLOWED_SPACES must not be empty")
        if self.confluence_auth_mode == "basic" and not self.confluence_username.strip():
            raise ValueError("CONFLUENCE_USERNAME is required for basic authentication")
        if self.confluence_auth_mode == "cookie" and self.confluence_cookie_file is None:
            raise ValueError("CONFLUENCE_COOKIE_FILE is required for cookie authentication")
        if self.confluence_auth_mode != "cookie" and not (
            self.confluence_token.get_secret_value().strip() or self.confluence_token_file
        ):
            raise ValueError("Confluence token or token file is required")
        return self


def read_secret(inline: SecretStr, path: Path | None, label: str) -> str:
    value = inline.get_secret_value().strip()
    if not value and path is not None:
        value = path.expanduser().read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError(f"{label} is empty")
    if len(value) > 16_384:
        raise RuntimeError(f"{label} is unexpectedly long")
    return value


def tls_verify(enabled: bool, ca_file: Path | None) -> bool | str:
    if not enabled:
        return False
    return str(ca_file.expanduser()) if ca_file else True


@lru_cache
def get_settings() -> Settings:
    return Settings()
