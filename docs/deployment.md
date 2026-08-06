# Server deployment

This document describes the internal server deployment of RAG Platform.

## Deployment identity

- Directory: `/opt/rag-platform`
- Compose project: `rag-platform`
- Compose file: `deploy/docker-compose.server.yml`
- Environment file: `.env`
- Web host port: `15173`
- API host port: `18000`

The real server `.env` file is not stored in Git.

## Services

The deployment contains eight services:

- `postgres`
- `redis`
- `minio`
- `embedding`
- `reranker`
- `api`
- `worker`
- `web`

Only API and web publish ports on the host.

PostgreSQL, Redis, MinIO, embedding and reranker are available only
inside the Compose network.

## Port mapping

Default loopback configuration:

```text
127.0.0.1:15173 -> web:5173
127.0.0.1:18000 -> api:8000
```

Direct internal-network access:

```text
<server-ip>:15173 -> web:5173
<server-ip>:18000 -> api:8000
```

The deployment must not occupy host ports:

```text
5173
8000
9000
```

Container ports with these numbers are allowed. A container port does
not occupy the same host port unless it is explicitly published.

## Environment setup

Create the server environment file:

```bash
cd /opt/rag-platform
cp .env.server.example .env
chmod 600 .env
```

Replace all placeholder passwords, tokens and endpoints.

Required project identity:

```dotenv
COMPOSE_PROJECT_NAME=rag-platform
ENVIRONMENT=server
```

For direct access from the internal network:

```dotenv
RAG_BIND_HOST=<server-ip>
RAG_API_HOST_PORT=18000
RAG_WEB_HOST_PORT=15173
WEB_ORIGIN=http://<server-ip>:15173
VITE_API_BASE_URL=http://<server-ip>:18000
```

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
cd /opt/rag-platform

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
  root@<server-ip>:/opt/rag-platform/
```

Do not add `--delete` without reviewing server-only files.

Then run on the server:

```bash
cd /opt/rag-platform

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
- all eight configured services;
- running containers;
- healthchecks;
- published ports;
- reserved host ports;
- API and web endpoints.

## Server reboot

Services use the Compose restart policy:

```yaml
restart: unless-stopped
```

After a coordinated server reboot:

```bash
cd /opt/rag-platform
scripts/deploy/compose-server.sh ps
scripts/deploy/check-server.sh
```

An SSH tunnel does not survive a disconnected SSH session or a server
reboot.

## PostgreSQL backup

```bash
backup_dir="/opt/rag-platform-backups/$(date +%Y%m%d-%H%M%S)"
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
  -v rag-platform_minio_data:/data:ro \
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

## Current limitation

The current web container uses the Vite development server.

It is acceptable for the server baseline, but it must be replaced with
a static production build and reverse proxy before the internal pilot.
