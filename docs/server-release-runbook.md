# v0.1 server release runbook

This runbook keeps the release project isolated from containers already running on the host. It never uses `docker system prune`, restarts Docker, deletes volumes, or runs `down -v`.

## 1. Send a read-only inventory

Copy the repository to a temporary or final application directory, then run:

```bash
cd /opt/rag-kbr-v01
RAG_WEB_HOST_PORT=15173 ./scripts/deploy/preflight-server.sh | tee /tmp/rag-kbr-preflight.txt
```

Send the contents of `/tmp/rag-kbr-preflight.txt`. It contains no environment variables or tokens. You may redact hostnames, public IP addresses and private registry names; keep GPU model, memory, driver version, Docker/Compose versions, container ports, network names and volume names visible.

Do not send `.env`, access tokens, OIDC client secrets, Confluence credentials, database passwords, or the output of `docker inspect`.

If port `15173` is occupied, choose a free high loopback port and repeat with that value. The application publishes only `127.0.0.1:RAG_WEB_HOST_PORT`; API, PostgreSQL, Redis, MinIO and model services remain inside the Compose network.

## 2. Prepare the release configuration

On the server, create `.env` from `.env.server.example` and fill secrets locally. The release identity must remain:

```text
COMPOSE_PROJECT_NAME=rag-kbr-v01
RAG_BIND_HOST=127.0.0.1
EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
EMBEDDING_DIM=384
RERANK_ENABLED=false
COMPOSE_PROFILES=
```

Register a public SPA client in corporate OIDC with Authorization Code + PKCE and no client secret. Configure the exact external URLs:

```text
OIDC_REDIRECT_URI=https://rag.example.com/auth/callback
OIDC_POST_LOGOUT_REDIRECT_URI=https://rag.example.com/
WEB_ORIGIN=https://rag.example.com
```

The issuer, audience mapper, tenant claim and role claim must match the API values in `.env`. Grant the tester the normal user role and the second account the `rag-admin` role in the same tenant.

## 3. Configure the existing reverse proxy

Point the selected virtual host at the chosen loopback web port. For nginx, the relevant location is:

```nginx
location / {
    proxy_pass http://127.0.0.1:15173;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_buffering off;
    proxy_read_timeout 300s;
}
```

Validate and reload only the existing proxy configuration using its normal operator procedure. Do not restart unrelated containers.

## 4. Build and start only the release project

```bash
cd /opt/rag-kbr-v01
./scripts/deploy/compose-server.sh config --quiet
./scripts/deploy/compose-server.sh build
./scripts/deploy/compose-server.sh up -d
./scripts/deploy/compose-server.sh ps
./scripts/deploy/check-server.sh
```

All commands are scoped by `-p rag-kbr-v01`. If a command reports a port or name collision, stop and inspect it; do not remove the existing owner.

## 5. Import the exact 298-page KBR snapshot

Mount/copy the frozen `.storage/confluence/KBR` directory to the host path in `KBR_SNAPSHOT_DIR`. Log in as the tester once so the default workspace exists, then find its ID with this read-only query:

```bash
./scripts/deploy/compose-server.sh exec -T postgres \
  sh -lc 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "select id, tenant_id, user_id, name from workspaces order by created_at;"'
```

First validate all files without writes:

```bash
./scripts/deploy/compose-server.sh exec -T api python -m app.operations.import_confluence_snapshot \
  --snapshot-root /snapshots/kbr \
  --workspace-id WORKSPACE_UUID \
  --expected-source https://confluence.skala-r.ru \
  --expected-space KBR \
  --expected-pages 298
```

Only after the command reports `validated ... pages=298`, repeat it with `--apply`. Do not add `--force` for the first import. Watch only this project:

```bash
./scripts/deploy/compose-server.sh logs -f --tail=100 worker api
```

Run `./scripts/deploy/check-server.sh` again after all 298 documents reach `READY`.

## 6. Acceptance and rollback

Check SSO for the tester and tenant admin, tenant isolation, upload, Confluence token entry/check/import, citations, follow-up questions, branch continuation, and streaming cancellation. Run the fixed KBR evaluation before promoting the RC tag.

Rollback application containers by checking out the prior tag and running `up -d --build` for the same project. Preserve the named volumes. A database downgrade is a separate reviewed operation and must never be improvised with volume deletion.

## 7. BGE-M3 on the following day

After reviewing GPU inventory, copy `deploy/bge-m3.env.example` to `deploy/bge-m3.env` and pin a compatible PyTorch CUDA image (preferably by digest). Validate the merged configuration:

```bash
./scripts/deploy/compose-bge.sh config --quiet
./scripts/deploy/compose-bge.sh config --services
```

The project name must be `rag-kbr-bge-ab`. Its database and model volumes are independent because Compose prefixes them with that project name. The BGE overlay does not start web unless profile `bge-ui` is explicitly enabled and uses `vector(1024)` only after verifying the isolated database is empty.
