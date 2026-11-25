#!/usr/bin/env bash
set -Eeuo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"
mkdir -p .storage/releases
archive="$(mktemp "$ROOT_DIR/.storage/releases/bge-release-XXXXXXXX.tar.gz")"
# Deliberate allow-list: no .env, snapshots, credentials, model weights or reports.
tar -czf "$archive" \
  apps/api/app/services/context_builder.py \
  apps/api/app/services/llm.py \
  apps/api/app/api/routes/answer.py \
  apps/api/app/evaluation/models.py \
  apps/api/app/evaluation/dataset.py \
  apps/api/app/evaluation/metrics.py \
  apps/web/src/App.tsx \
  apps/web/src/styles.css \
  scripts/deploy/compose-bge.sh \
  deploy/docker-compose.bge-release.yml \
  scripts/evaluation/check-bge-release.mjs \
  evaluation/splits/kbr-release-focus.txt \
  docs/bge-first-release.md
sha256sum "$archive"
printf 'Archive: %s\n' "$archive"
