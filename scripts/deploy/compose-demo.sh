#!/usr/bin/env bash
set -Eeuo pipefail
task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$task_root"
exec docker compose \
  --env-file "$task_root/.env.demo" \
  -p rag-zmostovoy \
  -f "$task_root/deploy/docker-compose.demo.yml" \
  "$@"
