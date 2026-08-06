#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(
  cd "$(dirname "${BASH_SOURCE[0]}")/../.." &&
  pwd
)"

COMPOSE_WRAPPER="${ROOT_DIR}/scripts/deploy/compose-server.sh"
ENV_FILE="${RAG_ENV_FILE:-${ROOT_DIR}/.env}"

REQUIRED_SERVICES=(
  postgres
  redis
  minio
  embedding
  reranker
  api
  worker
  web
)

failures=0

ok() {
  printf 'OK: %s\n' "$1"
}

info() {
  printf 'INFO: %s\n' "$1"
}

fail() {
  printf 'ERROR: %s\n' "$1" >&2
  failures=$((failures + 1))
}

read_env_value() {
  local key="$1"
  local fallback="$2"
  local value=""

  value="$(
    grep -E "^${key}=" "$ENV_FILE" 2>/dev/null |
      tail -n 1 |
      cut -d= -f2- |
      tr -d '\r'
  )"

  value="${value#\"}"
  value="${value%\"}"
  value="${value#\'}"
  value="${value%\'}"

  printf '%s' "${value:-$fallback}"
}

echo "===== RAG PLATFORM SERVER CHECK ====="
echo "Root: $ROOT_DIR"
echo "Env:  $ENV_FILE"
echo

if [[ ! -x "$COMPOSE_WRAPPER" ]]; then
  echo "ERROR: wrapper is not executable: $COMPOSE_WRAPPER" >&2
  exit 2
fi

if [[ ! -f "$ENV_FILE" ]]; then
  echo "ERROR: environment file not found: $ENV_FILE" >&2
  exit 2
fi

echo "===== DOCKER ====="

if docker info >/dev/null 2>&1; then
  ok "Docker daemon is available"
else
  echo "ERROR: Docker daemon is unavailable" >&2
  exit 2
fi

echo
echo "===== COMPOSE CONFIGURATION ====="

mapfile -t configured_services < <(
  "$COMPOSE_WRAPPER" config --services |
    sed '/^[[:space:]]*$/d' |
    sort -u
)

for service in "${REQUIRED_SERVICES[@]}"; do
  if printf '%s\n' "${configured_services[@]}" |
      grep -Fxq "$service"; then
    ok "service configured: $service"
  else
    fail "service missing from Compose: $service"
  fi
done

if [[ "${#configured_services[@]}" -eq "${#REQUIRED_SERVICES[@]}" ]]; then
  ok "Compose contains exactly 8 services"
else
  fail "expected 8 services, found ${#configured_services[@]}"
fi

echo
echo "===== CONTAINERS ====="

for service in "${REQUIRED_SERVICES[@]}"; do
  container_id="$(
    "$COMPOSE_WRAPPER" ps -q "$service" |
      head -n 1
  )"

  if [[ -z "$container_id" ]]; then
    fail "running container not found: $service"
    continue
  fi

  container_name="$(
    docker inspect \
      --format '{{.Name}}' \
      "$container_id" |
      sed 's#^/##'
  )"

  state="$(
    docker inspect \
      --format '{{.State.Status}}' \
      "$container_id"
  )"

  if [[ "$state" == "running" ]]; then
    ok "$service is running ($container_name)"
  else
    fail "$service state is $state ($container_name)"
    continue
  fi

  health="$(
    docker inspect \
      --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' \
      "$container_id"
  )"

  case "$health" in
    healthy)
      ok "$service healthcheck is healthy"
      ;;
    none)
      info "$service has no Docker healthcheck"
      ;;
    *)
      fail "$service healthcheck status is $health"
      ;;
  esac
done

echo
echo "===== PUBLISHED PORTS ====="

project_name="$(read_env_value COMPOSE_PROJECT_NAME rag-platform)"
bind_host="$(read_env_value RAG_BIND_HOST 127.0.0.1)"
api_host_port="$(read_env_value RAG_API_HOST_PORT 18000)"
web_host_port="$(read_env_value RAG_WEB_HOST_PORT 15173)"

export RAG_CHECK_PROJECT_NAME="$project_name"
export RAG_CHECK_BIND_HOST="$bind_host"
export RAG_CHECK_API_PORT="$api_host_port"
export RAG_CHECK_WEB_PORT="$web_host_port"

if python3 <<'PY'
import json
import os
import subprocess
import sys

project = os.environ["RAG_CHECK_PROJECT_NAME"]
bind_host = os.environ["RAG_CHECK_BIND_HOST"]
api_port = os.environ["RAG_CHECK_API_PORT"]
web_port = os.environ["RAG_CHECK_WEB_PORT"]

expected = {
    ("api", "8000/tcp"): (bind_host, api_port),
    ("web", "5173/tcp"): (bind_host, web_port),
}

container_ids = subprocess.check_output(
    [
        "docker",
        "ps",
        "-aq",
        "--filter",
        f"label=com.docker.compose.project={project}",
    ],
    text=True,
).split()

if not container_ids:
    print(
        f"ERROR: no containers found for project {project}",
        file=sys.stderr,
    )
    raise SystemExit(1)

containers = json.loads(
    subprocess.check_output(
        ["docker", "inspect", *container_ids],
        text=True,
    )
)

actual = {}
errors = []

for container in containers:
    labels = container.get("Config", {}).get("Labels") or {}
    service = labels.get("com.docker.compose.service", "unknown")
    name = container.get("Name", "").lstrip("/")

    ports = container.get("NetworkSettings", {}).get("Ports") or {}

    for container_port, bindings in ports.items():
        for binding in bindings or []:
            host_ip = binding.get("HostIp") or ""
            host_port = binding.get("HostPort") or ""

            print(
                f"published: service={service} "
                f"host={host_ip}:{host_port} "
                f"container={container_port}"
            )

            key = (service, container_port)

            if key in actual:
                errors.append(
                    f"multiple bindings found for "
                    f"{service}:{container_port}"
                )

            actual[key] = (host_ip, host_port)

            if key not in expected:
                errors.append(
                    f"unexpected published port: "
                    f"{name} {host_ip}:{host_port}->{container_port}"
                )

for key, expected_binding in expected.items():
    if key not in actual:
        errors.append(
            f"required port is not published: "
            f"{key[0]}:{key[1]}"
        )
        continue

    if actual[key] != expected_binding:
        errors.append(
            f"wrong binding for {key[0]}:{key[1]}: "
            f"expected {expected_binding[0]}:{expected_binding[1]}, "
            f"found {actual[key][0]}:{actual[key][1]}"
        )

if errors:
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    raise SystemExit(1)

print("Published ports: OK")
PY
then
  ok "only API and web ports are published"
else
  fail "published-port validation failed"
fi

echo
echo "===== RESERVED HOST PORTS ====="

reserved_ports="$(
  ss -H -lntp 2>/dev/null |
    grep -E ':(8000|5173|9000)([[:space:]]|$)' ||
    true
)"

if [[ -z "$reserved_ports" ]]; then
  ok "host ports 8000, 5173 and 9000 are free"
else
  fail "reserved host ports are occupied"
  printf '%s\n' "$reserved_ports" >&2
fi

echo
echo "===== HTTP ====="

probe_host="$bind_host"

case "$probe_host" in
  0.0.0.0|"::"|"[::]")
    probe_host="127.0.0.1"
    ;;
esac

if [[ "${RAG_SKIP_HTTP_CHECKS:-0}" == "1" ]]; then
  info "HTTP checks were skipped"
elif ! command -v curl >/dev/null 2>&1; then
  fail "curl is not installed"
else
  if curl \
    --fail \
    --silent \
    --show-error \
    --connect-timeout 5 \
    --max-time 15 \
    --output /dev/null \
    "http://${probe_host}:${api_host_port}/api/v1/health"; then
    ok "API health endpoint is available"
  else
    fail "API health endpoint is unavailable"
  fi

  if curl \
    --fail \
    --silent \
    --show-error \
    --connect-timeout 5 \
    --max-time 15 \
    --output /dev/null \
    "http://${probe_host}:${web_host_port}/"; then
    ok "web endpoint is available"
  else
    fail "web endpoint is unavailable"
  fi
fi

echo
echo "===== RESULT ====="

if [[ "$failures" -gt 0 ]]; then
  echo "Server check failed: $failures problem(s)" >&2
  exit 1
fi

echo "All server checks passed"
