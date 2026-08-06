#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(
  cd "$(dirname "${BASH_SOURCE[0]}")/../.." &&
  pwd
)"

ENV_FILE="${RAG_ENV_FILE:-${ROOT_DIR}/.env}"
COMPOSE_FILE="${RAG_COMPOSE_FILE:-${ROOT_DIR}/deploy/docker-compose.server.yml}"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: environment file not found: $ENV_FILE" >&2
  echo "Create it from .env.server.example." >&2
  exit 2
fi

if [[ ! -f "$COMPOSE_FILE" ]]; then
  echo "ERROR: Compose file not found: $COMPOSE_FILE" >&2
  exit 2
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif [[ -x /usr/local/bin/docker-compose ]]; then
  COMPOSE=(/usr/local/bin/docker-compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "ERROR: Docker Compose is not available" >&2
  exit 2
fi

project_name="${COMPOSE_PROJECT_NAME:-}"

if [[ -z "$project_name" ]]; then
  project_name="$(
    grep -E '^COMPOSE_PROJECT_NAME=' "$ENV_FILE" 2>/dev/null |
      tail -n 1 |
      cut -d= -f2- |
      tr -d '\r'
  )"
fi

project_name="${project_name#\"}"
project_name="${project_name%\"}"
project_name="${project_name#\'}"
project_name="${project_name%\'}"
project_name="${project_name:-rag-platform}"

if [[ ! "$project_name" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
  echo "ERROR: invalid Compose project name: $project_name" >&2
  exit 2
fi

cd "$ROOT_DIR"

exec "${COMPOSE[@]}" \
  --env-file "$ENV_FILE" \
  -p "$project_name" \
  -f "$COMPOSE_FILE" \
  "$@"
