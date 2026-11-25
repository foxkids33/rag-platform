#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${RAG_ENV_FILE:-${ROOT_DIR}/.env}"
TARGET_PORT="${RAG_WEB_HOST_PORT:-}"

if [[ -z "$TARGET_PORT" && -f "$ENV_FILE" ]]; then
  TARGET_PORT="$(grep -E '^RAG_WEB_HOST_PORT=' "$ENV_FILE" 2>/dev/null | tail -n 1 | cut -d= -f2- | tr -d '\r\"\047')"
fi
TARGET_PORT="${TARGET_PORT:-15173}"

if [[ ! "$TARGET_PORT" =~ ^[0-9]+$ ]] || (( TARGET_PORT < 1024 || TARGET_PORT > 65535 )); then
  echo "ERROR: invalid RAG_WEB_HOST_PORT: $TARGET_PORT" >&2
  exit 2
fi

echo "===== READ-ONLY SERVER PREFLIGHT ====="
echo "No containers, networks, volumes or ports will be changed."
echo

echo "===== GPU ====="
if command -v nvidia-smi >/dev/null 2>&1; then
  nvidia-smi --query-gpu=index,name,memory.total,memory.used,memory.free,driver_version --format=csv,noheader
  nvidia-smi --query-gpu=index,compute_cap --format=csv,noheader 2>/dev/null || true
else
  echo "INFO: nvidia-smi is not installed"
fi
echo

echo "===== DOCKER ====="
docker version --format 'server={{.Server.Version}}' 2>/dev/null || true
docker compose version 2>/dev/null || true
docker info --format 'runtimes={{json .Runtimes}}' 2>/dev/null || true
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}\t{{.Status}}'
echo

echo "===== NETWORKS AND VOLUMES ====="
docker network ls
docker volume ls
echo

echo "===== LISTENING PORTS ====="
ss -H -lntp 2>/dev/null || ss -H -lnt 2>/dev/null || true
echo

echo "===== TARGET PORT ====="
if ss -H -lnt 2>/dev/null | grep -Eq ":${TARGET_PORT}([[:space:]]|$)"; then
  echo "ERROR: requested loopback port ${TARGET_PORT} is already occupied" >&2
  exit 1
fi
echo "OK: requested loopback port ${TARGET_PORT} is free"
