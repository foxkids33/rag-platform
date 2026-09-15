# RAG Platform v0.1.0 — release candidate

Дата фиксации профиля: 2026-09-14.

## Назначение релиза

Первый контролируемый внутренний релиз для технических баз знаний. Он включает импорт пользовательских документов и Confluence, версионированные базы знаний, hybrid retrieval, grounded answers с проверяемыми ссылками, историю диалогов и ветвление разговора.

## Зафиксированный профиль

- Retrieval: dense + PostgreSQL FTS + exact identifier matching, объединение через weighted RRF.
- Reranker: выключен по умолчанию. Он остаётся экспериментальным Compose profile до повторного серверного A/B.
- Embeddings: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, FastEmbed `0.8.0`, mean pooling, 384 измерения; весь pilot workspace переиндексирован этим профилем, существующий индекс не меняется в день релиза.
- UI: отдельные разделы «Документы», «Confluence», «Базы знаний» и «Граф»; компактная история без вложенной прокрутки; фактический режим поиска обозначен явно.
- Authentication: браузерный OIDC Authorization Code + PKCE, runtime-конфигурация без пересборки image и access token только в `sessionStorage` текущей вкладки.
- Confluence credentials: пользователь вводит собственный e-mail/token, может проверить подключение; токен используется только в запросе и не сохраняется.
- Изоляция: tenant/workspace checks на уровне приложения. ACL отдельных исходных страниц Confluence не переносятся.

## Известные ограничения

- Релиз предназначен для доверенной внутренней аудитории, а не для публичного enterprise GA.
- Документы с разными исходными ACL нельзя смешивать в одном workspace.
- Нет PostgreSQL RLS, полного audit trail, HA/DR и production SLO.
- Короткие ссылочные follow-up вопросы зависят от rewrite + повторного retrieval; выбранная ветка передаёт историю, но не фиксирует прежний набор chunks.
- Расширенный multi-step search не входит в v0.1 и не показывается как доступный режим.

## Следующий серверный эксперимент

1. Развернуть `BAAI/bge-m3` на GPU как отдельное embedding space размерности 1024.
2. Выполнить полный reindex в новом индексе/версии, не смешивая векторы с текущими 384-dimensional embeddings.
3. На одном holdout сравнить dense-only и hybrid: R@1/5/10, MRR, nDCG, factual/citation coverage, abstention, p50/p95 и размер индекса.
4. Отдельно повторить reranker A/B и только после измерений выбирать модель, веса fusion и candidate limit.
5. Рассматривать late chunking как отдельный вариант эксперимента, а не как миграцию релизного индекса.

Пошаговая изоляция стенда, preflight без записи и матрица сравнений описаны в [BGE-M3 GPU server A/B](bge-m3-server-ab.md).

Архитектурные основания и полный список P0/P1/P2: [предрелизная ревизия](pre-release-architecture-review-2026-09-14.md).

## Проверка release candidate

Проверено 2026-09-15:

- web: TypeScript + Vite production build;
- API: Ruff без замечаний, `168 passed`, `4 skipped` без test database;
- worker: `18 passed`;
- embedding: `7 passed`, включая совместимость HTTP-контракта отдельного BGE-M3 backend;
- reranker: `2 passed`;
- local/server Compose config validation;
- live local smoke: все основные контейнеры стартуют, API и embedding healthy, фактические feature flags показывают hybrid/RRF включённым и reranker/advanced search выключенными;
- Confluence: персональный token contract, маскирование секрета, connection-check route, доступ владельца workspace и финальный UI render с реальными данными;
- embedding preflight: `1939` chunks обработаны без записи за `166.95 s`, service/database dimensions совпали (`384`), отчёт сохранён в `evaluation/results/kbr-embedding-preflight-minilm-rc1-20260915.json`.
- frozen KBR snapshot: все `298` page files прошли offline checksum/metadata/version/source-host validation без записи;
- server surface: наружу публикуется только loopback web port; API, PostgreSQL, Redis, MinIO и model services остаются во внутренней Compose-сети; reranker checker стал profile-aware.

Новый immutable Docker artifact пока не собран: последняя повторная попытка 2026-09-15 завершилась на внешней авторизации Docker Hub (`auth.docker.io: EOF`) ещё до сборки исходников. Локальная TypeScript/Vite production-сборка проходит. Перед tag/deploy нужно повторить image build, затем выполнить server smoke по [release runbook](server-release-runbook.md).
