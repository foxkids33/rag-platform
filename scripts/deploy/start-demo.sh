#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

bash scripts/deploy/setup-demo-env.sh
bash scripts/deploy/compose-demo.sh build embedding api worker web
# The pinned Chainguard MinIO runs as UID 65532. Initialize only this
# deployment's volume root; do not recursively change existing objects.
bash scripts/deploy/compose-demo.sh create minio
docker run --rm --user 0:0 \
  --mount type=volume,source=rag-zmostovoy_minio_data,target=/data \
  --entrypoint /bin/sh rag-zmostovoy-api \
  -c 'chown 65532:65532 /data && chmod 700 /data'
bash scripts/deploy/compose-demo.sh up -d
