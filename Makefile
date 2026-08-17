SHELL := /bin/bash

LOCAL_COMPOSE := docker compose \
	-f deploy/docker-compose.local.yml \
	-f deploy/docker-compose.embedding.yml \
	-f deploy/docker-compose.txt.yml
DOCLING_COMPOSE := $(LOCAL_COMPOSE) \
	-f deploy/docker-compose.docling.yml
FULL_COMPOSE := $(DOCLING_COMPOSE) \
	-f deploy/docker-compose.reranker.yml
RERANK_COMPOSE := $(LOCAL_COMPOSE) \
	-f deploy/docker-compose.reranker.yml

DATASET ?= /evaluation/datasets/skala-technical-reviews.v1.jsonl
CORPUS ?= /evaluation/corpora/skala-technical-reviews.v1.json
CASE_IDS ?=
DEV_CASE_IDS ?= /evaluation/splits/skala-technical-reviews.v1.dev.txt
ACCEPTANCE_CASE_IDS ?= /evaluation/splits/skala-technical-reviews.v1.acceptance.txt
EVALUATION_OUTPUT ?= /evaluation/results/latest.json
RERANK ?= false
OMIT_CASE_DETAILS ?= false
EVALUATION_TOKEN_ENV := $(if $(strip $(RAG_API_TOKEN)),-e RAG_API_TOKEN,)
EVALUATION_CORPUS_ARG := $(if $(strip $(CORPUS)),--corpus "$(CORPUS)",)
EVALUATION_CASE_IDS_ARG := $(if $(strip $(CASE_IDS)),--case-ids "$(CASE_IDS)",)
EVALUATION_RERANK_ARG := $(if $(filter true 1 yes,$(strip $(RERANK))),--rerank,--no-rerank)
EVALUATION_OMIT_CASE_DETAILS_ARG := $(if $(filter true 1 yes,$(strip $(OMIT_CASE_DETAILS))),--omit-case-details,)
BASELINE ?=
CANDIDATE ?= /evaluation/results/latest.json
GATES ?=
COMPARE_OUTPUT ?= /evaluation/results/comparison.json
FAILURE_REPORT ?= /evaluation/results/skala-v1-dev-latest.json
FAILURE_OUTPUT ?= /evaluation/results/skala-v1-dev-failures.json
FAILURE_MARKDOWN ?= /evaluation/results/skala-v1-dev-failures.md
DEV_GATES ?= /evaluation/quality-gates.dev-baseline.json
ACCEPTANCE_GATES ?= /evaluation/quality-gates.acceptance-baseline.json
COMPARE_BASELINE_ARG := $(if $(strip $(BASELINE)),--baseline "$(BASELINE)",)
COMPARE_GATES_ARG := $(if $(strip $(GATES)),--gates "$(GATES)",)

.PHONY: help local-env dev dev-docling dev-rerank dev-full down down-full logs ps ps-rerank
.PHONY: local-build local-build-docling local-build-rerank local-build-full local-test
.PHONY: evaluate evaluate-answers evaluate-dev evaluate-dev-check evaluate-acceptance evaluate-acceptance-check
.PHONY: evaluate-splits-check evaluate-failures evaluate-compare
.PHONY: api-test api-lint web-build smoke

help:
	@echo "make dev              - start local stack with embeddings"
	@echo "make dev-docling      - add PDF/DOCX parsing (large image)"
	@echo "make dev-rerank       - add the reranker without Docling"
	@echo "make dev-full         - add Docling and the reranker"
	@echo "make local-build      - rebuild the local stack without reranker"
	@echo "make local-build-docling - rebuild with PDF/DOCX parsing"
	@echo "make local-build-rerank - rebuild with reranker, without Docling"
	@echo "make local-build-full - rebuild the complete local stack"
	@echo "make local-test       - run API, worker and embedding tests in containers"
	@echo "make evaluate WORKSPACE_ID=<uuid> - run retrieval evaluation"
	@echo "make evaluate-answers WORKSPACE_ID=<uuid> - include LLM answers"
	@echo "make evaluate-dev WORKSPACE_ID=<uuid> - run the 72-case tuning split"
	@echo "make evaluate-acceptance WORKSPACE_ID=<uuid> - run the 24-case holdout"
	@echo "make evaluate-failures - classify the latest dev failures"
	@echo "make evaluate-splits-check - validate the 72/24 dataset partition"
	@echo "make evaluate-compare BASELINE=<json> CANDIDATE=<json> - detect regressions"
	@echo "make down             - stop the local stack"
	@echo "make logs             - follow local stack logs"
	@echo "make ps               - show local service status"
	@echo "make api-test         - run API tests on the host"
	@echo "make web-build        - build frontend on the host"
	@echo "make smoke            - run lightweight repository checks"

local-env:
	@if [[ ! -f .env ]]; then cp .env.example .env; echo "Created .env from .env.example"; fi
	@if grep -Eq '^EMBEDDING_DIM=1024([[:space:]]*)$$' .env; then \
		echo "ERROR: update EMBEDDING_DIM=1024 to EMBEDDING_DIM=384 in .env"; \
		exit 1; \
	fi

local-build: local-env
	$(LOCAL_COMPOSE) build

local-build-docling: local-env
	$(DOCLING_COMPOSE) build

local-build-rerank: local-env
	$(RERANK_COMPOSE) build

local-build-full: local-env
	$(FULL_COMPOSE) build

dev: local-env
	$(LOCAL_COMPOSE) up -d --build --remove-orphans

dev-docling: local-env
	$(DOCLING_COMPOSE) up -d --build --remove-orphans

dev-rerank: local-env
	$(RERANK_COMPOSE) up -d --build --remove-orphans

dev-full: local-env
	$(FULL_COMPOSE) up -d --build --remove-orphans

down:
	$(LOCAL_COMPOSE) down

down-full:
	$(FULL_COMPOSE) down

logs:
	$(LOCAL_COMPOSE) logs -f --tail=200

ps:
	$(LOCAL_COMPOSE) ps

ps-rerank:
	$(RERANK_COMPOSE) ps

local-test:
	$(LOCAL_COMPOSE) exec api python -m pytest -q
	$(LOCAL_COMPOSE) exec worker python -m pytest -q
	$(LOCAL_COMPOSE) exec embedding python -m pytest -q

evaluate:
	@test -n "$(WORKSPACE_ID)" || (echo "WORKSPACE_ID is required" && exit 2)
	$(LOCAL_COMPOSE) exec $(EVALUATION_TOKEN_ENV) api python -m app.evaluation.runner \
		--workspace-id "$(WORKSPACE_ID)" \
		--dataset "$(DATASET)" \
		$(EVALUATION_CASE_IDS_ARG) \
		$(EVALUATION_CORPUS_ARG) \
		$(EVALUATION_RERANK_ARG) \
		$(EVALUATION_OMIT_CASE_DETAILS_ARG) \
		--output "$(EVALUATION_OUTPUT)"

evaluate-answers:
	@test -n "$(WORKSPACE_ID)" || (echo "WORKSPACE_ID is required" && exit 2)
	$(LOCAL_COMPOSE) exec $(EVALUATION_TOKEN_ENV) api python -m app.evaluation.runner \
		--workspace-id "$(WORKSPACE_ID)" \
		--dataset "$(DATASET)" \
		$(EVALUATION_CASE_IDS_ARG) \
		$(EVALUATION_CORPUS_ARG) \
		$(EVALUATION_RERANK_ARG) \
		$(EVALUATION_OMIT_CASE_DETAILS_ARG) \
		--output "$(EVALUATION_OUTPUT)" \
		--answers

evaluate-dev:
	$(MAKE) evaluate-answers \
		WORKSPACE_ID="$(WORKSPACE_ID)" \
		CASE_IDS="$(DEV_CASE_IDS)" \
		RERANK=false \
		EVALUATION_OUTPUT=/evaluation/results/skala-v1-dev-latest.json
	$(MAKE) evaluate-failures

evaluate-dev-check: evaluate-dev
	$(MAKE) evaluate-compare \
		CANDIDATE=/evaluation/results/skala-v1-dev-latest.json \
		GATES="$(DEV_GATES)" \
		COMPARE_OUTPUT=/evaluation/results/skala-v1-dev-comparison.json

evaluate-acceptance:
	$(MAKE) evaluate-answers \
		WORKSPACE_ID="$(WORKSPACE_ID)" \
		CASE_IDS="$(ACCEPTANCE_CASE_IDS)" \
		RERANK=false \
		OMIT_CASE_DETAILS=true \
		EVALUATION_OUTPUT=/evaluation/results/skala-v1-acceptance-latest.json

evaluate-acceptance-check: evaluate-acceptance
	$(MAKE) evaluate-compare \
		CANDIDATE=/evaluation/results/skala-v1-acceptance-latest.json \
		GATES="$(ACCEPTANCE_GATES)" \
		COMPARE_OUTPUT=/evaluation/results/skala-v1-acceptance-comparison.json

evaluate-failures:
	$(LOCAL_COMPOSE) exec api python -m app.evaluation.failures \
		--report "$(FAILURE_REPORT)" \
		--output "$(FAILURE_OUTPUT)" \
		--markdown "$(FAILURE_MARKDOWN)"

evaluate-splits-check:
	$(LOCAL_COMPOSE) exec api python -m app.evaluation.splits \
		--dataset "$(DATASET)" \
		--dev "$(DEV_CASE_IDS)" \
		--acceptance "$(ACCEPTANCE_CASE_IDS)"

evaluate-compare:
	@test -n "$(BASELINE)$(GATES)" || (echo "BASELINE or GATES is required" && exit 2)
	$(LOCAL_COMPOSE) exec api python -m app.evaluation.compare \
		--candidate "$(CANDIDATE)" \
		$(COMPARE_BASELINE_ARG) \
		$(COMPARE_GATES_ARG) \
		--output "$(COMPARE_OUTPUT)"

api-test:
	cd apps/api && uv run --frozen python -m pytest

api-lint:
	cd apps/api && uv run --frozen ruff check .

web-build:
	cd apps/web && npm run build

smoke:
	python3 -m compileall apps/api/app apps/worker/app apps/embedding/app
	python3 -m json.tool apps/web/package.json >/dev/null
	python3 -m json.tool apps/web/tsconfig.json >/dev/null
	git diff --check

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
