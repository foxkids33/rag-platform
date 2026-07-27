# RAG Platform

Новый RAG-проект, который объединяет удобный UI текущей системы с более быстрым и точным retrieval-пайплайном.

## Целевые пользовательские режимы

1. Работа с готовой базой знаний.
2. Готовая база знаний + документы текущего workspace.
3. Только пользовательские документы без базовой базы знаний.

Во всех случаях запрос выполняется через одну модель `Workspace`: workspace может ссылаться на базовую KB и одновременно содержать собственные документы.

## Архитектура первого этапа

- `apps/api` — FastAPI API и orchestration.
- `apps/worker` — ingestion jobs (пока skeleton).
- `apps/web` — React/Vite UI в стиле существующего production UI.
- PostgreSQL + pgvector — метаданные, chunks, dense vectors, full-text index.
- Redis — очередь/координация фоновых задач.
- MinIO — оригинальные документы.
- Внешние сервисы — внутренний vLLM, embeddings и reranker endpoints.

## Запуск локально

```bash
cp .env.example .env
docker compose -f deploy/docker-compose.local.yml up --build
```

После запуска:

- UI: `http://localhost:5173`
- API: `http://localhost:8000`
- API health: `http://localhost:8000/api/v1/health`
- MinIO console: `http://localhost:9001`

## Разработка без Docker

API:

```bash
cd apps/api
uv sync
uv run uvicorn app.main:app --reload
```

UI:

```bash
cd apps/web
npm install
npm run dev
```

## Ближайшие этапы

1. Поднять инфраструктуру и применить миграции.
2. Реализовать CRUD knowledge bases и workspaces.
3. Добавить upload + ingestion worker.
4. Добавить Docling и hierarchical parent/child chunking.
5. Добавить embeddings + pgvector HNSW.
6. Добавить PostgreSQL FTS + RRF.
7. Добавить cross-encoder reranking.
8. Подключить внутренний vLLM и SSE streaming.
9. Добавить историю диалога.
10. Перенести граф исследования и добавить настоящий knowledge graph.

## Локальный reranker

Reranker запускается отдельным Compose overlay и переставляет hybrid-кандидатов
по прямой оценке пары «запрос + фрагмент». Если сервис недоступен, API возвращает
исходный RRF-порядок и заполняет поле `rerank_error`, не ломая поиск.

```bash
docker compose \
  -f deploy/docker-compose.local.yml \
  -f deploy/docker-compose.embedding.yml \
  -f deploy/docker-compose.reranker.yml \
  up -d --build reranker api
```

По умолчанию используется компактный multilingual cross-encoder. Модель можно
заменить через `RERANK_MODEL`, сохранив HTTP-контракт `/rerank`.

