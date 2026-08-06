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
12. Добавить quality gates: evidence scoring, diversity, citation audit и безопасный отказ.
13. Добавить управление workspace и режимами источников: user-only, KB-only и hybrid.
14. Добавить версии и публикацию готовых баз знаний.
15. Добавить ветвящийся граф диалогов и устойчивые прокручиваемые списки UI.
16. Добавить Neo4j и семантический knowledge graph.

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


## История диалогов и follow-up вопросы

Диалоги сохраняются в таблицах `chat_sessions` и `chat_messages`. API хранит
последние сообщения, источники и retrieval-метаданные. Для продолжения темы
короткий follow-up сначала преобразуется vLLM в самостоятельный поисковый
запрос, после чего выполняется обычный hybrid retrieval и grounded generation.
История используется только для разрешения контекста разговора; доказательства
текущего ответа всегда берутся из заново найденных источников.


## Контроль качества RAG

Перед генерацией context builder оценивает лучшие retrieval-кандидаты, удаляет
слабые и почти одинаковые фрагменты и ограничивает число источников из одного
документа. При недостаточных доказательствах API возвращает детерминированный
отказ и не вызывает vLLM. После генерации проверяется, что все ссылки `[n]`
относятся к реально переданным источникам. В JSON/SSE metadata доступны
`evidence_status`, `evidence_score`, `abstained` и `citation_valid`.


## Workspace и режимы источников

Workspace теперь явно хранит `source_mode`:

- `USER_DOCUMENTS` — retrieval только по документам текущего workspace;
- `KNOWLEDGE_BASE` — retrieval только по активной версии выбранной готовой базы;
- `HYBRID` — единый RRF + reranker по обоим слоям.

Workspace можно создавать, переименовывать и удалять через API и UI. При удалении
очищаются пользовательские документы в MinIO, chunks, embeddings и диалоги.
Источники ответа содержат provenance: документ workspace либо имя и версия базы
знаний. Неактивные версии KB никогда не участвуют в retrieval.


## Граф диалогов

Каждое сообщение хранит `parent_message_id`. Новый вопрос можно продолжить от
любого предыдущего ответа; в LLM и rewrite попадает только история выбранной
ветки. В интерфейсе доступны карточки, стрелки, панорамирование, масштабирование
и minimap. Списки диалогов, документов workspace и документов версии KB имеют
собственную прокрутку, поэтому кнопки действий остаются доступными при любом
числе элементов.

<!-- BEGIN SERVER DEPLOYMENT -->
## Server deployment

The shared-server deployment uses:

```text
deploy/docker-compose.server.yml
.env.server.example
scripts/deploy/compose-server.sh
scripts/deploy/check-server.sh
```

Validate the server configuration:

```bash
make server-config
make server-services
```

On the deployment server:

```bash
make server-ps
make server-check
```

Installation, updates, diagnostics, backup and shared-server safety are
documented in [docs/deployment.md](docs/deployment.md).

The current web container uses the Vite development server. A static
production build and reverse proxy are planned before the internal
pilot.
<!-- END SERVER DEPLOYMENT -->
