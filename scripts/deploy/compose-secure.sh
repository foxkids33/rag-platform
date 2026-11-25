#!/usr/bin/env bash
set -Eeuo pipefail
task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$task_root"
test -f "$task_root/deploy/docker-compose.secure.json" || {
  echo "Secure deployment override is missing; refusing an unauthenticated fallback." >&2
  exit 1
}
exec docker compose --env-file "$task_root/.env.demo" -p rag-zmostovoy \
  -f "$task_root/deploy/docker-compose.demo.yml" \
  -f "$task_root/deploy/docker-compose.secure.json" "$@"
