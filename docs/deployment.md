# Server deployment

This document describes the internal server deployment of RAG Platform.

## Deployment identity

- Directory: `/opt/rag-kbr-v01`
- Compose project: `rag-kbr-v01`
- Compose file: `deploy/docker-compose.server.yml`
- Environment file: `.env`
- Web host port: `15173`

The real server `.env` file is not stored in Git.

## Services

The baseline deployment contains seven services:

- `postgres`
- `redis`
- `minio`
- `embedding`
- `api`
- `worker`
- `web`

The optional `reranker` service appears only with the explicit `rerank` profile.
Only web publishes a loopback port on the host.

API, PostgreSQL, Redis, MinIO, embedding and reranker are available only
inside the Compose network.

## Port mapping

Default loopback configuration:

```text
127.0.0.1:15173 -> web:80
```

The existing reverse proxy is the only entry point and forwards the external
HTTPS virtual host to this loopback port. Other applications may use 5173,
8000, 9000 or any other ports; the read-only preflight only requires the chosen
`RAG_WEB_HOST_PORT` to be free.

## Environment setup

Create the server environment file:

```bash
cd /opt/rag-kbr-v01
cp .env.server.example .env
chmod 600 .env
```

Replace all placeholder passwords, tokens and endpoints.
`APP_ENV=server` now refuses to start the API with `AUTH_ENABLED=false`.
The browser UI performs OIDC Authorization Code + PKCE. Register a public SPA
client without a client secret and configure the exact external callback and
post-logout URLs. Runtime OIDC values are generated when the web container
starts and are not embedded in the frontend build.

Required project identity:

```dotenv
COMPOSE_PROJECT_NAME=rag-kbr-v01
ENVIRONMENT=server
```

For access through the existing reverse proxy:

```dotenv
RAG_BIND_HOST=127.0.0.1
RAG_WEB_HOST_PORT=15173
WEB_ORIGIN=https://rag.example.com
```

The static web container proxies browser requests under `/api/` to the internal
API service. Operators use `compose-server.sh exec` rather than a public API port.

API documentation uses the same proxy and external origin:

- Swagger UI: `/api/docs`
- OpenAPI JSON: `/api/openapi.json`
- ReDoc: `/api/redoc`

No additional published backend port is needed. The specification is generated
from the running API's routes and Pydantic models; see [API contract](api-contract.md).

Never commit `.env`.

## Compose wrapper

Use the wrapper for all project operations:

```bash
scripts/deploy/compose-server.sh
```

Examples:

```bash
scripts/deploy/compose-server.sh config
scripts/deploy/compose-server.sh ps
scripts/deploy/compose-server.sh logs --tail=100 api
```

The wrapper automatically selects an available Docker Compose
implementation and uses the correct environment, Compose file and
project name.

## Initial deployment

```bash
cd /opt/rag-kbr-v01

scripts/deploy/compose-server.sh config
scripts/deploy/compose-server.sh build
scripts/deploy/compose-server.sh up -d
scripts/deploy/check-server.sh
```

## Updating from a workstation

Run from the local repository:

```bash
rsync -az \
  --exclude='.git/' \
  --exclude='.env' \
  --exclude='.idea/' \
  --exclude='.venv/' \
  --exclude='node_modules/' \
  --exclude='dist/' \
  --exclude='__pycache__/' \
  --exclude='.pytest_cache/' \
  --exclude='.mypy_cache/' \
  --exclude='.ruff_cache/' \
  --exclude='*.pyc' \
  ./ \
  root@<server-ip>:/opt/rag-kbr-v01/
```

Do not add `--delete` without reviewing server-only files.

Then run on the server:

```bash
cd /opt/rag-kbr-v01

scripts/deploy/compose-server.sh config
scripts/deploy/compose-server.sh up -d --build
scripts/deploy/check-server.sh
```

## Recreating one service

API:

```bash
scripts/deploy/compose-server.sh \
  up -d --no-deps --force-recreate api
```

Worker:

```bash
scripts/deploy/compose-server.sh \
  up -d --no-deps --force-recreate worker
```

Web:

```bash
scripts/deploy/compose-server.sh \
  up -d --no-deps --force-recreate web
```

## Logs

```bash
scripts/deploy/compose-server.sh logs --tail=200
scripts/deploy/compose-server.sh logs --tail=200 api
scripts/deploy/compose-server.sh logs --tail=200 worker
scripts/deploy/compose-server.sh logs -f --tail=100 api
```

## Server check

```bash
scripts/deploy/check-server.sh
```

The check validates:

- Docker availability;
- all seven baseline services and reranker only when its profile is active;
- running containers;
- healthchecks;
- published ports;
- the single expected web loopback binding and absence of unexpected published ports;
- web and API health through the web proxy.

## Server reboot

Services use the Compose restart policy:

```yaml
restart: unless-stopped
```

After a coordinated server reboot:

```bash
cd /opt/rag-kbr-v01
scripts/deploy/compose-server.sh ps
scripts/deploy/check-server.sh
```

An SSH tunnel does not survive a disconnected SSH session or a server
reboot.

## PostgreSQL backup

```bash
backup_dir="/opt/rag-kbr-v01-backups/$(date +%Y%m%d-%H%M%S)"
install -d -m 700 "$backup_dir"

scripts/deploy/compose-server.sh exec -T postgres \
  sh -lc 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$backup_dir/postgres.dump"

test -s "$backup_dir/postgres.dump"
```

## MinIO backup

Use the same `backup_dir`:

```bash
docker run --rm \
  -v rag-kbr-v01_minio_data:/data:ro \
  -v "$backup_dir:/backup" \
  alpine:3.20 \
  tar -C /data -czf /backup/minio-data.tar.gz .
```

Create checksums:

```bash
sha256sum "$backup_dir"/* \
  > "$backup_dir/SHA256SUMS"
```

Backups must also be copied away from the application server.

Restore procedures must be tested separately before use on the only
working deployment.

## Shared-server restrictions

The server hosts unrelated applications.

Do not run:

```text
docker system prune
docker volume prune
docker network prune
docker container prune
```

Do not restart Docker, containerd or the server without coordination
with the server administrator.

Never run:

```bash
scripts/deploy/compose-server.sh down -v
```

The `-v` option deletes persistent project volumes.

## Web runtime

The server image builds the locked frontend dependencies with `npm ci`, serves
the static assets through Nginx, and disables proxy buffering for SSE requests.

For the first deployment, frozen KBR import and the isolated BGE-M3 A/B project,
follow [server-release-runbook.md](server-release-runbook.md).
