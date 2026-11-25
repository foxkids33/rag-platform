 #!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BASE_ENV_FILE="${RAG_ENV_FILE:-${ROOT_DIR}/.env}"
BGE_ENV_FILE="${RAG_BGE_ENV_FILE:-${ROOT_DIR}/deploy/bge-m3.env}"
BASE_COMPOSE="${ROOT_DIR}/deploy/docker-compose.server.yml"
BGE_COMPOSE="${ROOT_DIR}/deploy/docker-compose.bge-m3.yml"
RELEASE_ARGS=()
# Explicit opt-in: retain the existing BGE project and all its volumes. Only
# this fixed overlay may publish its web service on the existing pilot port.
if [[ "${1:-}" == "--release" ]]; then
  shift
  RELEASE_COMPOSE="${ROOT_DIR}/deploy/docker-compose.bge-release.yml"
  if [[ ! -f "$RELEASE_COMPOSE" ]]; then
    echo "ERROR: release Compose overlay is missing" >&2
    exit 2
  fi
  RELEASE_ARGS=(-f "$RELEASE_COMPOSE")
fi

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

read_setting() {
  local value
  value="$(awk -v key="$2" 'index($0, key "=") == 1 { value=substr($0,length(key)+2) } END { sub(/\r$/, "", value); print value }' "$1")"
  value="${value#\"}"; value="${value%\"}"
  value="${value#\'}"; value="${value%\'}"
  printf '%s' "$value"
}

project_name="$(read_setting "$BGE_ENV_FILE" COMPOSE_PROJECT_NAME)"
project_name="${project_name:-rag-kbr-bge-ab}"
if [[ ! "$project_name" =~ ^[a-z0-9][a-z0-9_-]*$ ]]; then
  echo "ERROR: invalid BGE Compose project name: $project_name" >&2
  exit 2
fi
release_project="${COMPOSE_PROJECT_NAME:-$(read_setting "$BASE_ENV_FILE" COMPOSE_PROJECT_NAME)}"
release_project="${release_project:-rag-kbr-v01}"
if [[ "$project_name" == "$release_project" || "$project_name" == "rag-kbr-v01" ]]; then
  echo "ERROR: BGE project must not reuse the release project name" >&2
  exit 2
fi

# Resolve the same precedence Compose uses; never source an environment file.
bge_image="${BGE_PYTORCH_IMAGE:-$(read_setting "$BGE_ENV_FILE" BGE_PYTORCH_IMAGE)}"
bge_gpu="${BGE_GPU_DEVICE_ID:-$(read_setting "$BGE_ENV_FILE" BGE_GPU_DEVICE_ID)}"
bge_database="${BGE_DATABASE_URL:-$(read_setting "$BGE_ENV_FILE" BGE_DATABASE_URL)}"
if [[ -z "$bge_image" || "$bge_image" == *REPLACE* || -z "$bge_gpu" || "$bge_gpu" == *REPLACE* ]]; then
  echo "ERROR: configure a compatible BGE_PYTORCH_IMAGE and available BGE_GPU_DEVICE_ID first" >&2
  exit 2
fi
if [[ ! "$bge_database" =~ ^postgresql\+asyncpg://[^/@]+@postgres:5432/[^/?#]+$ || "$bge_database" == *REPLACE* ]]; then
  echo "ERROR: BGE_DATABASE_URL must target only the isolated postgres:5432 service (no placeholders)" >&2
  exit 2
fi
for argument in "$@"; do
  case "$argument" in
    -p|--project-name|--project-name=*|-f|--file|--file=*|--env-file|--env-file=*|--profile|--profile=*)
      echo "ERROR: overriding the isolated Compose configuration is not supported" >&2
      exit 2
      ;;
  esac
done
export RAG_SERVICE_ENV_FILE="$BASE_ENV_FILE"
export COMPOSE_PROFILES=""
export BGE_PYTORCH_IMAGE="$bge_image" BGE_GPU_DEVICE_ID="$bge_gpu" BGE_DATABASE_URL="$bge_database"
cd "$ROOT_DIR"
exec "${COMPOSE[@]}" \
  --env-file "$BASE_ENV_FILE" \
  --env-file "$BGE_ENV_FILE" \
  -p "$project_name" \
  -f "$BASE_COMPOSE" \
  -f "$BGE_COMPOSE" \
  "${RELEASE_ARGS[@]}" \
  "$@"
