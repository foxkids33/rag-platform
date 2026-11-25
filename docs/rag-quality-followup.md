# Исправления после сравнения BGE / MiniLM

Изменения локальные. Сервер и его данные автоматически не менялись.

## Что изменено

- Контекст Confluence собирается сначала из компактных найденных фрагментов.
  Полный родительский раздел добавляется только в оставшийся бюджет, если он
  содержит выбранный фрагмент и не помечен как обрезанный. Уже выбранные источники
  при расширении не вытесняются. Общий лимит контекста сохранён.
- Промпт требует ответить на все части вопроса, перечислить подтверждённые
  проверки результата и не путать алгоритм с уровнем/флагом настройки.
- UI отправляет выбранный лимит 800/2048/4096 (по умолчанию 2048), а не постоянные
  800 токенов. Это максимум, не обязательная длина ответа.
- Настоящий `finish_reason` из потока LLM передаётся в SSE и сохраняется в истории.
  При `length` UI показывает предупреждение, в том числе после перезагрузки.
- `accepted_citation_filenames` разрешает явно проверенную альтернативу единственному
  эталонному источнику **только для citation_source_coverage**. Recall, MRR и
  context_source_coverage по-прежнему используют исходный gold. Автоматического
  признания всех `copy_` эквивалентными нет.

## Версия разметки v3

Исходный `evaluation/results/kbr.v1.prepared-v2.jsonl` и старые отчёты не изменены.
Новый `kbr.v1.prepared-v3.jsonl` находится рядом, также исключён из Git.

В v3 изменены только три случая:

- `kbr-017`, `kbr-018`: для цитирования допускается TLS copy 189210517 v1 —
  в просмотренных фрагментах есть именно запрошенные команды. Это не утверждение
  об эквивалентности всей страницы или её будущих версий.
- `kbr-022`: числа узлов допускают явные альтернативы `5||пять`, `2||два||две`,
  `3||три`. Общая логика сравнения чисел, версий, портов и команд не ослаблена.

Воспроизводимое создание нового файла (не перезаписывает существующий):

```bash
node scripts/evaluation/prepare-kbr-v3.mjs \
  evaluation/results/kbr.v1.prepared-v2.jsonl \
  evaluation/results/kbr.v1.prepared-v3.jsonl
```

Скрипт проверяет SHA-256 входного v2:
`caae95822112223f2a0d32910c72a6175443b06711895274b020273357d05b3f`.
SHA-256 созданного v3:
`c941265e7aa2653278a7243e83d95ae8ef60c9787a848e550872fdc2e3338082`.
При другом снимке TLS-копию нужно проверить заново.

Не сравнивайте старые метрики v2 с новыми v3 как чистый эффект исправления RAG.
Сначала повторите **v2** на новом API — это сравнение поведения на неизменной
разметке. V3 используйте отдельно и одинаково для обеих моделей. Набор остаётся
разработческим, а не независимым приёмочным тестом.

## Ручное обновление сервера

Все команды выполняет оператор. Не заменять `.env`, `deploy/bge-m3.env`, модели,
снимки и volumes. Переиндексация для этих правок не нужна. Обновление API кратко
прервёт активные запросы; выполняйте после завершения генераций.

Передайте в `/root/rag-kbr-v01` следующие изменённые исходники, сохранив старые
копии и image ID для отката:

```text
apps/api/app/services/context_builder.py
apps/api/app/services/llm.py
apps/api/app/api/routes/answer.py
apps/api/app/evaluation/models.py
apps/api/app/evaluation/dataset.py
apps/api/app/evaluation/metrics.py
apps/web/src/App.tsx
apps/web/src/styles.css
```

V3 передавайте отдельно в `evaluation/results/`, если нужен прогон по новой
разметке. Полный архив текущего дерева включает также другие незавершённые
изменения проекта — для этого обновления достаточно перечисленных файлов.

На сервере, только после проверки совпадения проектов:

```bash
cd /root/rag-kbr-v01
export COMPOSE_PROJECT_NAME=rag-kbr-v01
bash scripts/deploy/compose-server.sh ps
bash scripts/deploy/compose-bge.sh ps

bash scripts/deploy/compose-server.sh build api web
bash scripts/deploy/compose-bge.sh build api

bash scripts/deploy/compose-server.sh up -d --no-deps --no-build api
bash scripts/deploy/compose-server.sh up -d --no-deps --no-build --force-recreate web
bash scripts/deploy/compose-bge.sh up -d --no-deps --no-build api

curl --fail --show-error --max-time 10 http://127.0.0.1:15173/api/v1/workspaces
```

Пересоздание web обновляет разрешение имени API в nginx. Не нужны `down`, удаление
volumes, обновление worker/embedding или перезапуск внешнего vLLM. Проверьте логи
API и открытие старой рабочей области перед оценкой.

## Повторная проверка на прежней разметке

Используйте новые имена отчётов, сохранив старые для сравнения:

```bash
bash scripts/deploy/compose-bge.sh exec -T api \
  python -m app.evaluation.runner \
  --base-url http://localhost:8000 \
  --workspace-id b15f3afe-55c0-4fa5-9c6c-1fe17779cade \
  --dataset /evaluation/results/kbr.v1.prepared-v2.jsonl \
  --mode hybrid --limit 10 --no-rerank \
  --answers --answer-max-tokens 4096 --answer-temperature 0 --fail-fast \
  --output /evaluation/results/bge-m3-hybrid-answers-4096-context-v2.json

docker cp evaluation/results/kbr.v1.prepared-v2.jsonl \
  rag-kbr-v01-api-1:/tmp/kbr.v1.prepared-v2.jsonl
bash scripts/deploy/compose-server.sh exec -T api \
  python -m app.evaluation.runner \
  --base-url http://localhost:8000 \
  --workspace-id 1e50a10f-d92d-4634-a4b3-27de04475345 \
  --dataset /tmp/kbr.v1.prepared-v2.jsonl \
  --mode hybrid --limit 10 --no-rerank \
  --answers --answer-max-tokens 4096 --answer-temperature 0 --fail-fast \
  --output /tmp/minilm-hybrid-answers-4096-context-v2.json &&
docker cp rag-kbr-v01-api-1:/tmp/minilm-hybrid-answers-4096-context-v2.json \
  evaluation/results/minilm-hybrid-answers-4096-context-v2.json
```

Сравнить все случаи, особенно `kbr-026` (в контексте и ответе условие включения
сжатия корзины и ZSTD), `kbr-016` (Uptime **и** тестовые запросы), `kbr-027`
(нет ложного отказа), девять случаев точных команд. Проверить новые пропуски,
цитаты, отказы и задержку, а не только среднее покрытие фактов.

Локальные тесты не доказывают улучшение ответов реальной LLM. Это установит
повторный серверный прогон. UI по-прежнему запрашивает rerank и retrieval_limit=8,
поэтому его результат не следует напрямую отождествлять с этим eval без rerank.
