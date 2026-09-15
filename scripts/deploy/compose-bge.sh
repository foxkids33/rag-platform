#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BASE_ENV_FILE="${RAG_ENV_FILE:-${ROOT_DIR}/.env}"
BGE_ENV_FILE="${RAG_BGE_ENV_FILE:-${ROOT_DIR}/deploy/bge-m3.env}"
BASE_COMPOSE="${ROOT_DIR}/deploy/docker-compose.server.yml"
BGE_COMPOSE="${ROOT_DIR}/deploy/docker-compose.bge-m3.yml"

for required in "$BASE_ENV_FILE" "$BGE_ENV_FILE" "$BASE_COMPOSE" "$BGE_COMPOSE"; do
  if [[ ! -f "$required" ]]; then
    echo "ERROR: required file not found: $required" >&2
    exit 2
  fi
done

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "ERROR: Docker Compose is not available" >&2
  exit 2
fi

project_name="$(grep -E '^COMPOSE_PROJECT_NAME=' "$BGE_ENV_FILE" | tail -n 1 | cut -d= -f2- | tr -d '\r\"\047')"
project_name="${project_name:-rag-kbr-bge-ab}"
if [[ ! "$project_name" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
  echo "ERROR: invalid BGE Compose project name: $project_name" >&2
  exit 2
fi
if [[ "$project_name" == "rag-kbr-v01" ]]; then
  echo "ERROR: BGE project must not reuse the release project name" >&2
  exit 2
fi

export RAG_SERVICE_ENV_FILE="${RAG_SERVICE_ENV_FILE:-$BASE_ENV_FILE}"
cd "$ROOT_DIR"
exec "${COMPOSE[@]}" \
  --env-file "$BASE_ENV_FILE" \
  --env-file "$BGE_ENV_FILE" \
  -p "$project_name" \
  -f "$BASE_COMPOSE" \
  -f "$BGE_COMPOSE" \
  "$@"
