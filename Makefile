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
