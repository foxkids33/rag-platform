SHELL := /bin/bash

.PHONY: help dev down logs api-test api-lint web-build smoke

help:
	@echo "make dev       - start local docker stack"
	@echo "make down      - stop local docker stack"
	@echo "make logs      - follow stack logs"
	@echo "make api-test  - run API tests"
	@echo "make web-build - build frontend"
	@echo "make smoke     - run lightweight repository checks"

dev:
	docker compose -f deploy/docker-compose.local.yml up --build

down:
	docker compose -f deploy/docker-compose.local.yml down

logs:
	docker compose -f deploy/docker-compose.local.yml logs -f --tail=200

api-test:
	cd apps/api && uv run pytest

api-lint:
	cd apps/api && uv run ruff check .

web-build:
	cd apps/web && npm run build

smoke:
	python3 -m compileall apps/api/app apps/worker/app
	python3 -m json.tool apps/web/package.json >/dev/null
	python3 -m json.tool apps/web/tsconfig.json >/dev/null

# BEGIN SERVER DEPLOYMENT
.PHONY: server-config server-services server-ps server-check
.PHONY: server-up server-deploy server-logs
.PHONY: server-recreate-api server-recreate-worker
.PHONY: server-recreate-web

server-config:
	./scripts/deploy/compose-server.sh config

server-services:
	./scripts/deploy/compose-server.sh config --services

server-ps:
	./scripts/deploy/compose-server.sh ps

server-check:
	./scripts/deploy/check-server.sh

server-up:
	./scripts/deploy/compose-server.sh up -d

server-deploy:
	./scripts/deploy/compose-server.sh up -d --build

server-logs:
	./scripts/deploy/compose-server.sh logs -f --tail=200

server-recreate-api:
	./scripts/deploy/compose-server.sh up -d --no-deps --force-recreate api

server-recreate-worker:
	./scripts/deploy/compose-server.sh up -d --no-deps --force-recreate worker

server-recreate-web:
	./scripts/deploy/compose-server.sh up -d --no-deps --force-recreate web
# END SERVER DEPLOYMENT
