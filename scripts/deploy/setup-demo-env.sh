#!/usr/bin/env bash
set -Eeuo pipefail

task_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
task_env="$task_root/.env.demo"
if [[ -e "$task_env" ]]; then
  printf 'Existing demo environment preserved: %s\n' "$task_env"
  exit 0
fi
umask 077
task_postgres_secret="$(openssl rand -hex 24)"
task_minio_secret="$(openssl rand -hex 24)"
sed \
  -e "s/REPLACE_POSTGRES_SECRET/$task_postgres_secret/" \
  -e "s/REPLACE_MINIO_SECRET/$task_minio_secret/" \
  "$task_root/deploy/demo.env.example" > "$task_env"
chmod 600 "$task_env"
unset task_postgres_secret task_minio_secret
printf 'Created private demo environment: %s\n' "$task_env"
