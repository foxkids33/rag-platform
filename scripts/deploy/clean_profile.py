"""Local-only, dedicated clean-install configuration and source packaging.

Uses stdlib only; never imports application settings, reads a previous .env,
sources shell assignments, resolves model endpoints or contacts Confluence.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tarfile
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE = Path("deploy/clean.env.example")
COMPOSE = Path("deploy/docker-compose.clean.yml")
COMMANDS = {
    "config": ["config", "--quiet"],
    "build": ["build"],
    "up": ["up", "-d", "--build"],
    "down": ["down"],
    "logs": ["logs", "--tail", "100"],
    "ps": ["ps"],
}


class CleanError(ValueError):
    pass


def parse_env(path: Path) -> dict[str, str]:
    """Restricted literal dotenv; no shell interpolation, includes or quoting."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, sep, value = line.partition("=")
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key) or key in values:
            raise CleanError("Configuration contains a malformed or repeated key")
        if value != value.strip() or any(c in value for c in "$`\\\"'\x00"):
            raise CleanError(f"{key} must be a literal unquoted value without shell syntax")
        if any(ord(c) < 32 for c in value):
            raise CleanError(f"{key} contains control characters")
        values[key] = value
    return values


def require_url(values: dict[str, str], key: str, *, https: bool = False) -> None:
    try:
        parts = urlsplit(values.get(key, ""))
        port = parts.port
    except ValueError:
        raise CleanError(f"{key} must be a valid endpoint URL") from None
    if (parts.scheme not in ({"https"} if https else {"http", "https"})
            or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment or port == 0
            or any(c.isspace() for c in values.get(key, ""))):
        raise CleanError(f"{key} must be an endpoint URL without credentials, query or fragment")


def validate_env(path: Path, *, root: Path = ROOT, connections: bool = True) -> dict[str, str]:
    if path.name == ".env" or path.is_symlink():
        raise CleanError("Use a dedicated clean-install file, never .env or a symlink")
    if not path.is_file() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise CleanError("Dedicated configuration must exist with private permissions (chmod 600)")
    values = parse_env(path)
    known = parse_env(root / TEMPLATE)
    if set(values) - set(known):
        raise CleanError("Configuration contains unsupported keys")
    if values.get("RAG_CLEAN_CONFIG_VERSION") != "1":
        raise CleanError("Configuration must be generated for this clean profile")
    if not re.fullmatch(r"rag-clean-[a-z0-9][a-z0-9-]{5,48}", values.get("COMPOSE_PROJECT_NAME", "")):
        raise CleanError("COMPOSE_PROJECT_NAME must identify a dedicated rag-clean-* project")
    for key, minimum in [("POSTGRES_PASSWORD", 32), ("MINIO_ACCESS_KEY", 16), ("MINIO_SECRET_KEY", 32)]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{%d,128}" % minimum, values.get(key, "")):
            raise CleanError(f"{key} must contain a newly generated local service secret")
    if values.get("RAG_BIND_HOST") != "127.0.0.1":
        raise CleanError("This profile publishes web only on 127.0.0.1; use an HTTPS reverse proxy for OIDC")
    if not values.get("RAG_WEB_HOST_PORT", "").isdigit() or not 1024 <= int(values["RAG_WEB_HOST_PORT"]) <= 65535:
        raise CleanError("RAG_WEB_HOST_PORT must be between 1024 and 65535")
    mode = values.get("RAG_CLEAN_MODE")
    if mode not in {"pilot", "oidc"}:
        raise CleanError("Choose RAG_CLEAN_MODE pilot or oidc explicitly")
    expected_auth = "true" if mode == "oidc" else "false"
    expected_pilot = "false" if mode == "oidc" else "true"
    if values.get("AUTH_ENABLED") != expected_auth or values.get("AUTH_ALLOW_UNAUTHENTICATED_PILOT") != expected_pilot:
        raise CleanError("Authentication settings do not match the selected clean-install mode")
    # Building or stopping a dedicated stack must work before external access exists.
    if not connections:
        return values
    for key in ["VLLM_BASE_URL", "EMBEDDING_BASE_URL", "WEB_ORIGIN"]:
        require_url(values, key)
    if urlsplit(values["WEB_ORIGIN"]).path not in {"", "/"}:
        raise CleanError("WEB_ORIGIN must be an origin without a path")
    for key in ["VLLM_MODEL", "EMBEDDING_MODEL", "CONFLUENCE_ALLOWED_SPACES"]:
        if not values.get(key):
            raise CleanError(f"{key} is required before using the clean profile")
    if not re.fullmatch(r"[1-9][0-9]{0,3}", values.get("EMBEDDING_DIM", "")) or int(values["EMBEDDING_DIM"]) > 2000:
        raise CleanError("EMBEDDING_DIM must match the external service and be between 1 and 2000")
    require_url(values, "CONFLUENCE_BASE_URL", https=True)
    if values.get("CONFLUENCE_DEPLOYMENT") not in {"cloud", "data_center"}:
        raise CleanError("Choose CONFLUENCE_DEPLOYMENT cloud or data_center")
    if values.get("CONFLUENCE_AUTH_MODE") not in {"basic", "bearer"}:
        raise CleanError("Choose CONFLUENCE_AUTH_MODE basic or bearer")
    space_pattern = r"(?:[A-Za-z0-9_-]{1,255}|~[A-Za-z0-9_][A-Za-z0-9_.@-]{0,253})"
    spaces = values["CONFLUENCE_ALLOWED_SPACES"].split(",")
    if len(spaces) > 1000 or any(not re.fullmatch(space_pattern, item.strip()) for item in spaces):
        raise CleanError("Choose an explicit nonempty Confluence space allowlist")
    credential_keys = (["CONFLUENCE_EMAIL", "CONFLUENCE_API_TOKEN"]
                       if values["CONFLUENCE_AUTH_MODE"] == "basic" else ["CONFLUENCE_ACCESS_TOKEN"])
    if any(not values.get(key) for key in credential_keys):
        raise CleanError("Configure the dedicated Confluence server account for the selected authentication mode")
    if mode == "pilot":
        expected_origin = f"http://127.0.0.1:{values['RAG_WEB_HOST_PORT']}"
        if (values.get("AUTH_ENABLED") != "false"
                or values.get("AUTH_ALLOW_UNAUTHENTICATED_PILOT") != "true"
                or values.get("WEB_ORIGIN") != expected_origin):
            raise CleanError("Pilot requires explicit unauthenticated opt-in and its loopback origin")
    elif mode == "oidc":
        if values.get("AUTH_ENABLED") != "true" or values.get("AUTH_ALLOW_UNAUTHENTICATED_PILOT") != "false":
            raise CleanError("OIDC mode must enable authentication without a pilot exception")
        for key in ["OIDC_ISSUER", "OIDC_JWKS_URL", "WEB_ORIGIN", "OIDC_REDIRECT_URI", "OIDC_POST_LOGOUT_REDIRECT_URI"]:
            require_url(values, key, https=True)
        for key in ["OIDC_AUDIENCE", "OIDC_CLIENT_ID"]:
            if not values.get(key):
                raise CleanError(f"{key} is required for OIDC")
        if values.get("OIDC_ALGORITHM") not in {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512", "EdDSA"}:
            raise CleanError("Choose an asymmetric OIDC signing algorithm")
    return values


def setup(output: Path, mode: str, *, root: Path = ROOT) -> None:
    if mode not in {"pilot", "oidc"}:
        raise CleanError("Choose pilot or oidc explicitly")
    if output.name == ".env" or output.is_symlink():
        raise CleanError("Choose a new dedicated clean configuration path")
    values = parse_env(root / TEMPLATE)
    values.update({
        "COMPOSE_PROJECT_NAME": "rag-clean-" + secrets.token_hex(6),
        "POSTGRES_PASSWORD": secrets.token_hex(32),
        "MINIO_ACCESS_KEY": "rag" + secrets.token_hex(12),
        "MINIO_SECRET_KEY": secrets.token_hex(32),
        "RAG_CLEAN_MODE": mode,
        "AUTH_ENABLED": "true" if mode == "oidc" else "false",
        "AUTH_ALLOW_UNAUTHENTICATED_PILOT": "false" if mode == "oidc" else "true",
    })
    if mode == "oidc":
        values["WEB_ORIGIN"] = ""
    # O_EXCL refuses overwrite, including an existing installation's configuration.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write("# Dedicated clean install; configure external endpoints before up.\n")
        stream.writelines(f"{key}={value}\n" for key, value in values.items())
    print(f"Created private {mode} configuration: {output}")
    print("Fill external LLM, embeddings, Confluence allowlist and any OIDC settings before validation.")


def compose(env_file: Path, command: str, *, root: Path = ROOT, allow_local_pilot: bool = False) -> int:
    if command not in COMMANDS:
        raise CleanError("Unsupported clean-install command")
    values = validate_env(env_file, root=root, connections=command in {"config", "up"})
    if command == "up" and values["RAG_CLEAN_MODE"] == "pilot" and not allow_local_pilot:
        raise CleanError("Starting the loopback pilot requires explicit --allow-local-pilot")
    env = os.environ.copy()
    # Process values cannot override the selected file or redirect Compose to another stack.
    for key in set(parse_env(root / TEMPLATE)) | {k for k in env if k.startswith("COMPOSE_")}:
        env.pop(key, None)
    if env.get("DOCKER_HOST") and not env["DOCKER_HOST"].startswith(("unix://", "npipe://")):
        raise CleanError("Remote DOCKER_HOST is forbidden for the clean-install wrapper")
    result = subprocess.run(["docker", "context", "inspect"], env=env, capture_output=True, text=True, check=True)
    try:
        endpoint = json.loads(result.stdout)[0]["Endpoints"]["docker"]["Host"]
    except (ValueError, IndexError, KeyError, TypeError):
        raise CleanError("Cannot verify that the selected Docker context is local") from None
    if not endpoint.startswith(("unix://", "npipe://")):
        raise CleanError("Selected Docker context is remote; only local sockets are allowed")
    env.update(values)
    # Relative build contexts in this file are based on deploy/, not repo root.
    args = ["docker", "compose", "--project-directory", str(root / COMPOSE.parent),
            "--env-file", str(env_file.resolve()), "-p", values["COMPOSE_PROJECT_NAME"],
            "-f", str(root / COMPOSE), *COMMANDS[command]]
    result = subprocess.run(args, env=env, cwd=root, check=False)
    if command == "config" and result.returncode == 0:
        print("Clean configuration is valid; secrets were not printed.")
    return result.returncode


def package_files(root: Path) -> list[Path]:
    """Explicit code/document allowlist, never whole-repository archiving."""
    files: set[Path] = set()
    exact = [TEMPLATE, COMPOSE, Path(".gitignore"), Path("CLEAN_INSTALL.md"), Path("docs/confluence-clean-deployment.md"),
             Path("apps/object_store/Dockerfile.clean"), Path("apps/object_store/.dockerignore")]
    for script in ["clean_profile.py", "compose-clean.sh", "setup-clean-env.sh", "package-clean.sh"]:
        exact.append(Path("scripts/deploy") / script)
    for application in ["api", "worker"]:
        base = Path("apps") / application
        exact.extend(base / name for name in ["Dockerfile.clean", ".dockerignore", "pyproject.toml", "uv.lock"])
        exact.append(base / "app/main.py")
        files.update(p.relative_to(root) for p in (root / base / "app").rglob("*.py"))
    exact.append(Path("apps/api/alembic.ini"))
    files.update(p.relative_to(root) for p in (root / "apps/api/alembic").rglob("*") if p.suffix in {".py", ".mako"})
    web = root / "apps/web"
    for pattern in ["Dockerfile", ".dockerignore", "package.json", "package-lock.json", "tsconfig*.json", "vite*.ts", "index*.html", "nginx*.conf", "runtime-config.js.template"]:
        files.update(p.relative_to(root) for p in web.glob(pattern))
    for path in (web / "src").rglob("*"):
        if path.suffix in {".ts", ".tsx", ".css", ".svg"}:
            files.add(path.relative_to(root))
    for name in ["runtime-config.js", "favicon.svg", "favicon.ico"]:
        if (web / "public" / name).is_file():
            files.add(Path("apps/web/public") / name)
    for name in ["LICENSE", "LICENSE.md", "LICENSE.txt"]:
        if (root / name).is_file():
            files.add(Path(name))
    for path in exact:
        if not (root / path).is_file():
            raise CleanError(f"Required clean-package source is missing: {path}")
        files.add(path)
    for path in files:
        if any(root.joinpath(*path.parts[:index]).is_symlink() for index in range(1, len(path.parts) + 1)) or "__pycache__" in path.parts:
            raise CleanError("Package contains a symlink or generated cache")
        if any(part in {".env", ".storage", ".venv", "node_modules", "dist", "presentation", "evaluation"} for part in path.parts):
            # Evaluation source modules under apps/api/app are code, not corpus data.
            if not (path.parts[:4] == ("apps", "api", "app", "evaluation") and path.suffix == ".py"):
                raise CleanError("Package contains forbidden runtime or demonstration content")
    return sorted(files)


def package(output: Path, *, root: Path = ROOT) -> None:
    paths = package_files(root)
    # Hash exactly the same bytes that enter the archive, even if an editor saves.
    snapshot = {path: (root / path).read_bytes() for path in paths}
    manifest = {str(path): hashlib.sha256(data).hexdigest() for path, data in snapshot.items()}
    checksum_path = Path(str(output) + ".sha256")
    if output.exists() or output.is_symlink() or checksum_path.exists() or checksum_path.is_symlink():
        raise CleanError("Package output and checksum paths must be new files")
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as raw, tarfile.open(fileobj=raw, mode="w:gz") as archive:
        for path, data in snapshot.items():
            info = tarfile.TarInfo(str(path))
            info.size = len(data)
            info.mode = 0o755 if path.suffix == ".sh" else 0o644
            archive.addfile(info, io.BytesIO(data))
        data = (json.dumps({"format": 1, "purpose": "clean Confluence source install", "sha256": manifest}, indent=2) + "\n").encode()
        info = tarfile.TarInfo("CLEAN_SOURCE_MANIFEST.json")
        info.size, info.mode = len(data), 0o644
        archive.addfile(info, io.BytesIO(data))
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    fd = os.open(checksum_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(f"{checksum}  {output.name}\n")
    print(f"Created source package: {output}")
    print(f"SHA-256: {checksum}")
    print(f"Inventory with per-file SHA-256 is inside CLEAN_SOURCE_MANIFEST.json; checksum: {checksum_path}")
    print("No images were built and no services were started.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    create = commands.add_parser("setup")
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--mode", choices=["pilot", "oidc"], required=True)
    run = commands.add_parser("compose")
    run.add_argument("--env-file", type=Path, required=True)
    run.add_argument("--allow-local-pilot", action="store_true")
    run.add_argument("command", choices=COMMANDS)
    bundle = commands.add_parser("package")
    bundle.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "setup":
            setup(args.output, args.mode)
        elif args.action == "compose":
            return compose(args.env_file, args.command, allow_local_pilot=args.allow_local_pilot)
        else:
            package(args.output)
    except (CleanError, OSError, subprocess.SubprocessError) as exc:
        # Never render config content or command environments in error messages.
        message = str(exc) if isinstance(exc, CleanError) else type(exc).__name__
        print(f"Clean install: {message}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
