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
9. Подключить streaming-ответ и источники к интерфейсу чата.
10. Добавить управление документами workspace: исключение из поиска и полное удаление.
11. Добавить историю диалога и контекстные follow-up вопросы.
12. Перенести граф исследования и добавить настоящий knowledge graph.

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


### Устойчивое reranking-ранжирование

Перед отправкой кандидатов в cross-encoder API удаляет повторяющиеся колонтитулы,
номера страниц, строки оглавления, URL и email, затем выбирает короткий фрагмент,
сфокусированный на терминах запроса. Итоговый порядок использует не абсолютное
значение насыщенного `rerank_score`, а объединение `rerank_rank` с исходным
`retrieval_rank`. Титульные страницы и оглавления получают отдельный штраф.

Диагностические поля ответа:

- `rerank_rank` — позиция только по cross-encoder;
- `rerank_fusion_score` — итоговый score после объединения рангов;
- `rerank_penalty` — штраф за титульный или оглавительный блок.


## Управление документами workspace

Готовый документ можно временно исключить из retrieval без удаления исходника,
чанков и embeddings. Повторное включение не требует переиндексации. Полное
удаление очищает объект в MinIO и строку документа в PostgreSQL; связанные
chunks и ingestion jobs удаляются каскадно. Документы в статусах `QUEUED` и
`PROCESSING` нельзя удалять до завершения ingestion.
