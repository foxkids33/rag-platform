#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUTPUT_DIR="${1:-$ROOT_DIR/.storage/releases}"
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"
ARCHIVE="$(mktemp "$OUTPUT_DIR/rag-pilot-XXXXXXXX.tar.gz")"
trap 'if [[ -n "${ARCHIVE:-}" ]]; then rm -f -- "$ARCHIVE"; fi' EXIT
cd "$ROOT_DIR"

# Include the current working tree, including new connector/migration files.
# Do not require a commit or include .git, actual env files, models or snapshots.
{
  git ls-files -z --cached --others --exclude-standard -- \
    apps deploy scripts docs evaluation README.md Makefile .env.example .env.server.example
  printf '%s\0' apps/confluence_connector/.env.connector.example
} | sort -zu | while IFS= read -r -d '' path; do
  [[ -f "$path" && ! -L "$path" ]] || continue
  case "$path" in
    .env.example|.env.server.example|apps/confluence_connector/.env.connector.example) ;;
    .env|.env.*|*/.env|*/.env.*|deploy/bge-m3.env|*/credential.json|*.pem|*.key) continue ;;
  esac
  case "$path" in
    */.venv/*|*/node_modules/*|*/dist/*|*/__pycache__/*|*/.pytest_cache/*|*/.ruff_cache/*|evaluation/results/*) continue ;;
  esac
  printf '%s\0' "$path"
done | tar --null --verbatim-files-from -T - -czf "$ARCHIVE"

sha256sum "$ARCHIVE"
printf 'Source archive: %s\nNo server connection was made.\n' "$ARCHIVE"
ARCHIVE=""
