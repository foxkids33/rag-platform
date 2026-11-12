"""Fail closed before starting the server connector; never display a secret."""

from __future__ import annotations

import json
import os
import stat
import sys
import uuid
from pathlib import Path


def private_file(path: Path, *, writable: bool = False) -> None:
    metadata = path.lstat()
    allowed_modes = {0o600} if writable else {0o400, 0o600}
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_nlink != 1
        or stat.S_IMODE(metadata.st_mode) not in allowed_modes
        or not 1 <= metadata.st_size <= 16_384
    ):
        raise ValueError("Private credential must be an owner-only regular file")


def main() -> int:
    try:
        from confluence_connector.config import Settings, normalize_rag_url

        settings = Settings(_env_file=None)
        if (
            settings.confluence_auth_mode != "bearer"
            or settings.confluence_token.get_secret_value()
        ):
            raise ValueError("Server connector requires a service PAT file, not an inline secret")
        if settings.rag_enrollment_token.get_secret_value():
            raise ValueError("Enroll the server machine once before starting this service")
        if not settings.rag_verify_ssl or not settings.confluence_verify_ssl:
            raise ValueError("TLS verification must remain enabled")
        if settings.rag_allow_insecure_http:
            raise ValueError("The server connector must use HTTPS")
        if not settings.confluence_token_file or not settings.rag_credential_file:
            raise ValueError("Explicit protected credential files are required")
        import re

        if any(not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", key) for key in settings.allowed_spaces):
            raise ValueError("Server service account requires approved non-personal space keys")
        if sys.argv[1:] not in ([], ["--check"]):
            raise ValueError("Server profile only accepts its normal startup or --check")
        private_file(settings.confluence_token_file)
        private_file(settings.rag_credential_file, writable=True)
        state = settings.rag_credential_file.parent.lstat()
        if (not stat.S_ISDIR(state.st_mode) or state.st_uid != os.getuid()
                or stat.S_IMODE(state.st_mode) != 0o700):
            raise ValueError("Machine credential directory must be private and service-owned")
        credential = json.loads(settings.rag_credential_file.read_text(encoding="utf-8"))
        uuid.UUID(credential.get("connector_id", ""))
        token = credential.get("credential")
        if not isinstance(token, str) or not 16 <= len(token) <= 512:
            raise ValueError("Machine credential is invalid")
        if normalize_rag_url(credential.get("rag_url", "")) != normalize_rag_url(settings.rag_url):
            raise ValueError("Machine credential belongs to another RAG destination")
        for path in (settings.rag_ca_file, settings.confluence_ca_file):
            if path is None or not path.is_file() or not os.access(path, os.R_OK):
                raise ValueError("Readable trusted CA files are required")
    except Exception:
        print(
            "Server connector preflight failed: check files, permissions, spaces and TLS; "
            "no secret was displayed.", file=sys.stderr,
        )
        return 1
    os.umask(0o077)
    # No --all-accessible-spaces and no --enroll-pilot in a permanent service.
    os.execv(sys.executable, [sys.executable, "-m", "confluence_connector", *sys.argv[1:]])
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
