# Предрелизная архитектурная ревизия — 2026-09-14

## Итоговый вердикт

Текущая система уже подходит для **контролируемого внутреннего релиза v0.1** после короткого этапа P0-hardening. Это не прототип: в проекте есть надёжный ingestion, гибридный поиск, иерархические чанки, grounded generation, проверка цитат, версионирование базы знаний, tenant/workspace-изоляция на уровне приложения и полноценный evaluation-контур.

Позиционировать v0.1 как готовую **enterprise multi-tenant RAG platform** пока рано. Основные причины: не переносятся ACL отдельных страниц и документов, нет полноценной end-to-end наблюдаемости и аудита, production-конфигурация не совпадает с проверенной конфигурацией evaluation, а release/deployment-контур ещё недостаточно зафиксирован и воспроизводим.

Late chunking, Kafka/Flink и переход на отдельную vector database **не должны блокировать v0.1**. Они имеют смысл только после измерений на независимом наборе запросов и появления требований к масштабу или свежести данных, которых текущая архитектура уже не сможет обеспечить.

Ревизия основана на коде, конфигурации, документации и сохранённых результатах evaluation. 2026-09-15 локальный Compose-стек был поднят без reranker: migrations, API health/system info, embedding readiness, Confluence status/validation, CORS и web UI проверены на текущих исходниках через read-only dev mounts. Сборка новых Docker images дважды остановилась на внешнем EOF Docker Hub; server artifact и server smoke остаются отдельным шагом выкладки.

## Что уже сделано хорошо

| Область | Реализация | Оценка |
|---|---|---|
| Ingestion | PostgreSQL — источник истины для job queue; lease, heartbeat, recovery, retry; Redis используется только как ускоритель пробуждения; удаление объектов проводится через outbox | Сильная и практичная основа для v0.1. Отказ Redis не приводит к потере заданий |
| Парсинг и чанки | Структурный Confluence parser; Docling для PDF/DOCX; атомарные code blocks; parent/child chunks; заголовки и breadcrumbs включаются в поисковое представление | Хорошая предметная реализация. Основной риск — несогласованность размеров чанков с embedding-моделью |
| Retrieval | Dense pgvector + PostgreSQL FTS + exact identifier matching + weighted RRF + опциональный reranker + parent/neighbor expansion | Архитектурно зрелее типового baseline. Хорошо подходит для технической документации, где важны и смысл, и точные идентификаторы |
| Generation | Ответ только по контексту, явные citations, partial answer, abstention, защита от инструкций из источника, постпроверка цитат, buffered streaming | Хорошая защита от выдуманных ссылок и преждевременной выдачи непроверенного ответа |
| База знаний | Неизменяемые версии и публикация `DRAFT → INDEXING → READY → ACTIVE`, архивирование и rollback | Сильный release-механизм для контента и безопасного полного reindex |
| Изоляция | `tenant_id`, workspace/KB scoping, OIDC-проверки, cross-tenant ответы скрываются через 404 | Подходит для внутреннего релиза при строгом разделении аудиторий по workspace. Не заменяет document-level ACL и PostgreSQL RLS |
| Quality loop | Метрики retrieval, factual coverage, citations, abstention, code/order и latency; dev/acceptance split поддержан инструментами | Хорошая платформа для развития. Текущий KBR-набор пока мал и составлен разработчиком, поэтому не является независимым holdout |
| CI | Lint, миграции и migration check, API/worker/model tests, web build | Хорошая база. До production-релиза не хватает Compose smoke, image/security scanning и воспроизводимого release artifact |

Текущий полный KBR-прогон из 36 случаев успешен: retrieval `R@5 = 0.9375`, `R@10 = 0.96875`, `MRR = 0.798735`, required-fact coverage `0.934722`; корректны 32/32 ответов для answerable и 4/4 отказов для unanswerable. Это хороший сигнал для внутреннего релиза, но не достаточное доказательство обобщающей способности системы.

## Embeddings и chunking

### Главная проблема текущей конфигурации

Embedding service использует `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`: 384 измерения. Развёрнутая FastEmbed/ONNX-конфигурация объявляет усечение на 512 входных токенах; это не равно 512 словам и не гарантирует, что текущая политика чанков согласована с моделью. При этом:

- Docling создаёт чанки до 450 токенов, причём границы рассчитываются tokenizer-моделью BGE-M3;
- plain text и Confluence допускают до 2200 символов;
- некоторые code blocks намеренно остаются атомарными и превышают общий лимит;
- tokenizer для чанков и tokenizer embedding-модели различаются, поэтому лимит в 450 токенов на этапе chunking не гарантирует отсутствие усечения на embedding-этапе;
- атомарные code blocks могут превышать общий лимит.

Следствие: хвост длинного чанка не влияет на dense vector после достижения фактического лимита tokenizer. Синтетическая проверка текущего runtime показала исчезновение влияния позднего русского текста примерно после 128 повторяющихся слов из-за разбиения на subword tokens. Гибридный FTS/exact retrieval и parent expansion способны скрывать эту проблему в итоговых метриках. Поэтому сильный hybrid-результат ещё не доказывает качество именно dense retrieval. Размерность 384 указана в [model card MiniLM](https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2); фактический порог усечения следует измерять на runtime tokenizer и реальном корпусе.

### Решение для v0.1

Не менять embedding-модель вслепую перед релизом. До фиксации v0.1 нужно:

1. Считать фактическое распределение длины `search_text` тем tokenizer, который применяет embedding-модель: p50/p95/p99, долю усечённых чанков и отдельно code/table chunks.
2. Сделать усечение наблюдаемым: сохранять или логировать `token_count`, `was_truncated`, модель и ревизию модели. В v0.1 неработающий вариант `truncate=false` уже отклоняется явно, а health endpoint публикует настроенный context limit.
3. Для новой индексной версии проверить MiniLM с child chunks примерно 96–112 токенов, сохранив крупный parent context. Атомарный код при необходимости индексировать отдельными логическими частями, но возвращать пользователю целый блок или parent.
4. Отдельно измерить dense-only, lexical-only и hybrid режимы. Иначе FTS продолжит маскировать ошибки embeddings.

Это минимальный безопасный путь: он не меняет релизную модель данных и позволяет понять, является ли MiniLM реальным ограничением именно на этом корпусе.

### Следующий эксперимент с моделью

Основной кандидат для A/B — BGE-M3: multilingual-модель с контекстом до 8192 токенов и dense/sparse/multi-vector режимами. Её dense vector имеет 1024 измерения, тогда как текущая колонка PostgreSQL зафиксирована как `vector(384)`. Поэтому смена модели — не переключатель окружения, а новая схема/индекс, полный reindex и отдельная версия embedding space. Характеристики приведены в [официальной model card BGE-M3](https://huggingface.co/BAAI/bge-m3).

Минимальная матрица A/B:

| Вариант | Chunk strategy | Что проверяет |
|---|---|---|
| A | MiniLM; tokenizer-aligned child chunks 96–112 токенов; текущий parent expansion | Потенциал текущей дешёвой модели без потери хвоста |
| B | BGE-M3 dense; child chunks 256/384/512 токенов; текущий parent expansion | Эффект более сильной long-context multilingual модели |
| C | Long-context embedding + late chunking на spans 128/256 токенов | Добавочную ценность контекста документа при построении вектора каждого чанка |

Для каждого варианта нужен один и тот же независимый корпус запросов: `R@1/5/10`, MRR, nDCG, fact coverage, citation/source coverage, abstention, code accuracy, ingestion throughput, размер индекса и online p95. Победителя следует выбирать по end-to-end качеству и стоимости, а не по публичному embedding benchmark.

### Нужно ли внедрять late chunking сейчас

Нет, не как обязательную часть v0.1.

Late chunking сначала кодирует длинный документ до token embeddings и только затем объединяет token spans в векторы чанков. Это сохраняет контекст документа, потерянный при независимом кодировании каждого чанка. Метод требует long-context модели, token-level output и точного соответствия offsets документа и чанков; текущий FastEmbed endpoint возвращает только готовые pooled vectors, поэтому внедрение не будет локальной заменой функции. См. [оригинальную работу Late Chunking](https://arxiv.org/abs/2409.04701).

У проекта уже есть более дешёвая форма contextual retrieval: breadcrumbs/headers в `search_text`, небольшие child chunks для поиска и крупные parents/neighbors для ответа. Late chunking следует добавлять только как вариант C в A/B. Работа также не показывает универсального превосходства на каждом датасете, поэтому архитектурное усложнение без собственного замера неоправданно.

## PostgreSQL/pgvector или отдельная vector database

Для текущего масштаба — около 1,939 импортированных KBR-чанков — PostgreSQL + pgvector является разумным выбором: одна транзакционная система для metadata, versions, FTS, vectors и job lifecycle, меньше компонентов и проще восстановление.

Текущий HNSW-индекс выбран корректно, но при росте нужно отдельно нагрузочно проверять фильтрованный поиск по tenant/workspace/KB. В pgvector approximate index filtering выполняется после index scan; официальная документация предлагает iterative scans, partial indexes или partitioning, когда фильтры начинают снижать recall. См. [документацию pgvector](https://github.com/pgvector/pgvector).

Триггеры для пересмотра решения:

- миллионы/десятки миллионов активных чанков и заметное ухудшение p95 или recall после tenant-фильтров;
- необходимость независимого масштабирования vector workload;
- multi-vector/ColBERT retrieval становится обязательным;
- эксплуатационная команда готова поддерживать ещё одну stateful систему.

До появления одного из этих условий миграция на Qdrant/Milvus/другую vector database добавит больше операционной сложности, чем ценности.

## Сравнение с enterprise RAG-архитектурами

[Архитектура Confluent](https://www.confluent.io/blog/enterprise-knowledge-management-with-rag-for-digital-native-companies/) строится вокруг event streaming: CDC/connectors → Kafka → Flink enrichment/redaction → embeddings/vector DB → ACL-aware retrieval → LLM, с lineage, governance и observability. Это полезная целевая модель для больших компаний с большим количеством постоянно изменяющихся источников, но не минимальное определение enterprise RAG.

| Возможность | Confluent reference | Текущий проект | Решение |
|---|---|---|---|
| Надёжная асинхронная доставка | Kafka, connectors, exactly-once patterns | PostgreSQL durable queue + lease/retry, Redis wake-up, deletion outbox | Достаточно для v0.1. Сохранить стабильные event envelopes, Kafka вводить при требованиях к throughput/freshness |
| Изменения источников | CDC и потоковые connectors | Upload/import и Confluence sync | Достаточно для статической базы знаний; нет near-real-time ingestion множества источников |
| Обработка | Flink pipelines, enrichment, PII redaction | Worker parsing/chunking/embedding | Хорошая модульность, но нет PII/DLP pipeline и policy enforcement |
| Авторизация | Metadata/ACL в событиях и query-time RBAC | Tenant/workspace authorization | Ключевой enterprise gap: ACL страницы/документа не переносятся из Confluence |
| Retrieval | Vector DB и metadata filtering | pgvector + FTS + exact + RRF + rerank | Текущий вариант сильнее простого vector-only RAG и соответствует масштабу |
| Provenance | Governance, lineage, audit | Source/page/version metadata и KB versions | Хорошее начало; нужен неизменяемый аудит доступа, ingestion и административных действий |
| Operations | Streaming observability and governance | Health endpoint и логи, ограниченная readiness/telemetry | Нужны метрики, tracing, alerting, SLO и dependency readiness |

Критическая разница не в отсутствии Kafka. Для enterprise-готовности важнее security trimming: права документа должны быть записаны при индексировании и применены при каждом запросе. Это же требование подчёркивает [руководство Azure AI Search по document-level access](https://learn.microsoft.com/en-us/azure/search/search-security-best-practices).

## Риски production-конфигурации

1. **Evaluation/profile drift.** Профиль v0.1 зафиксирован как `rerank=false`; server Compose и шаблон окружения теперь используют его по умолчанию. Температура генерации и прочие параметры должны быть сверены с сохранённым acceptance-отчётом перед внешним развёртыванием.
2. **ACL scope.** Confluence page/group restrictions не импортируются. После импорта доступ определяется workspace. Нельзя смешивать в одном workspace документы для разных аудиторий.
3. **Frontend auth.** Backend OIDC реализован, но production-путь входа требует identity-aware proxy либо завершённого frontend OIDC flow.
4. **Readiness.** API health endpoint не проверяет DB, object storage, embedding/reranker и LLM. Compose readiness частично опирается на запуск процесса, а не готовность зависимости.
5. **Deployment reproducibility.** Есть floating image tags, часть Python images работает от root, установка `uv` не закреплена версией; миграция БД запускается вместе со стартом API и может конфликтовать при нескольких репликах.
6. **Single-node operations.** PostgreSQL, MinIO и Redis используют локальные volumes; инструкции backup/restore есть, но автоматизация, проверенные восстановления, HA и SLO отсутствуют.
7. **Release state.** Большой объём текущей реализации находится в dirty worktree, а roadmap README отстаёт от кода. Такой snapshot нельзя надёжно воспроизвести или откатить до фиксации коммитов, версии и image tags.

## P0 — до контролируемого внутреннего v0.1

- [x] Зафиксировать release profile v0.1: hybrid dense + FTS + exact matching + RRF, `rerank=false`. Reranker вынесен в явный Compose profile для A/B.
- [ ] Собрать независимый smoke/acceptance набор хотя бы из 20–30 запросов от будущих пользователей, не участвовавших в составлении текущих 36 кейсов.
- [ ] Зафиксировать исходники: разложить изменения на обозримые коммиты, обновить version/changelog, получить clean release commit и immutable tag; привязать к нему image tags и evaluation report.
- [ ] Проверить реальный OIDC end-to-end. Ограничить v0.1 одной доверенной аудиторией на workspace; явно задокументировать отсутствие source ACL propagation.
- [ ] Сделать production secrets обязательными, исключить использование примерных значений; закрепить версии базовых images, model artifacts и `uv`. Для сетевого production deployment перевести контейнеры на non-root до релиза.
- [ ] Выполнить server Compose smoke с чистого состояния: migrations, ingestion, search, answer/citations, restart worker во время job, отказ Redis, backup и test restore.
- [ ] Добавить dependency readiness и минимальные операционные сигналы: request/job correlation ID, queue depth/age, ingestion failures/retries, search/LLM latency, citation rejection, dependency errors и disk usage.
- [ ] Измерить долю усечения embeddings на реальном корпусе. Контракт `truncate=false` теперь отклоняется явно, а известное ограничение записано; смена модели остаётся на P1.
- [x] Удалить устаревшее состояние `hybrid_retrieval: planned`; интерфейс и README больше не утверждают, что reranker включён в релизный профиль.

Если v0.1 разворачивается только в изолированной внутренней сети, часть container/supply-chain hardening допустимо оформить как формально принятую задолженность с владельцем и сроком. Для доступного извне production deployment эти пункты остаются блокирующими.

## P1 — сразу после v0.1

- Провести описанный embedding/chunking A/B и выбрать модель на собственном holdout.
- Сделать embedding model revision и chunking policy неизменяемой частью версии индекса; поддержать параллельное построение нового embedding space и атомарное переключение.
- Внедрить централизованные metrics/traces/dashboard/alerts и определить SLO для ingestion freshness, search p95 и answer success.
- Автоматизировать backup/restore drills и добавить Compose/integration smoke в CI или release pipeline.
- Добавить SBOM, dependency/container scanning и release provenance.
- Улучшить query/retrieval diagnostics: вклад dense/FTS/exact, stage latency, причины пустого результата и reranker delta.
- Нагрузочно проверить pgvector с реальными tenant-фильтрами; при необходимости включить iterative scans/partitioning.

## P2 — условия для enterprise GA

- Source-native document/page/group ACL propagation и query-time enforcement; желательно дополнительная защита через PostgreSQL RLS или изолированные индексы для строгих tenants.
- PII/DLP classification, redaction и policy-based routing до помещения текста в embedding/LLM pipeline.
- Неизменяемый audit trail для чтения, поиска, генерации, ingestion, публикации версий и административных действий.
- HA/DR с измеренными RPO/RTO, rolling deployment, отдельным migration job и backward-compatible migrations.
- Квоты, rate limiting, per-tenant capacity controls, cost accounting и abuse monitoring.
- Event backbone/CDC только для источников, где доказанно нужны near-real-time freshness, большой fan-out или независимые consumers.
- Регулярная экспертная и adversarial evaluation: prompt injection, sensitive retrieval, cross-tenant leakage, stale citations и unsupported answers.

## Рекомендуемая формулировка первого релиза

> v0.1 — внутренняя RAG-система для технической базы знаний с надёжным импортом документов, гибридным поиском, версионированной публикацией, grounded answers и проверяемыми citations. Релиз рассчитан на заранее определённые группы пользователей и не переносит индивидуальные ACL исходных страниц.

Не заявлять в v0.1: универсальную enterprise multi-tenancy, near-real-time knowledge fabric, автоматическую DLP/PII-защиту или доказанно оптимальную embedding-модель.
