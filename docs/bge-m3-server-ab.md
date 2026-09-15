# BGE-M3 GPU: безопасный серверный A/B

Этот эксперимент не является миграцией релизного индекса v0.1. Текущий production-like профиль остаётся на `paraphrase-multilingual-MiniLM-L12-v2` и `vector(384)` до прохождения quality gates.

## Почему нельзя заменить только переменную модели

- `BAAI/bge-m3` возвращает dense-вектор размерности 1024, а текущая колонка `document_chunks.embedding` имеет тип `vector(384)`.
- Существующий embedding-контейнер устанавливает CPU `onnxruntime`, не резервирует GPU в Compose и поэтому не задействует CUDA. Для использования VRAM нужен отдельный GPU backend/image.
- Векторы разных моделей нельзя сравнивать или частично обновлять в одном embedding space. Нужны отдельная БД/индекс и полный reindex одного и того же корпуса.

Reindex-команда теперь проверяет readiness модели и фактический тип колонки до генерации векторов. При несовпадении 1024/384 она завершается без записи.

## 1. Инвентаризация сервера

До выбора CUDA image зафиксировать:

```bash
nvidia-smi --query-gpu=name,compute_cap,memory.total,driver_version --format=csv
docker version
docker compose version
docker info --format '{{json .Runtimes}}'
```

Нужны модель GPU, compute capability, объём VRAM и версия драйвера. По ним выбирается совместимый CUDA/PyTorch image. Не следует подбирать image вслепую на рабочем Compose-проекте.

## 2. Изоляция эксперимента

Для BGE-M3 используется отдельный Compose project `rag-kbr-bge-ab`. Он не использует volumes релизного проекта `rag-kbr-v01` и по умолчанию вообще не запускает web-сервис.

Минимальные обязательные отличия стенда:

```text
COMPOSE_PROJECT_NAME=rag-kbr-bge-ab
EMBEDDING_MODEL=BAAI/bge-m3
EMBEDDING_NATIVE_DIM=1024
EMBEDDING_DIM=1024
RERANK_ENABLED=false
```

Перед загрузкой документов схема отдельной БД должна содержать `document_chunks.embedding vector(1024)`, а API и worker должны быть запущены с `EMBEDDING_DIM=1024`. Изменять тип колонки в БД релизного стенда запрещено.

Готовый wrapper `scripts/deploy/compose-bge.sh` объединяет release Compose с `deploy/docker-compose.bge-m3.yml`, берёт секреты из основного `.env`, а не-секретные A/B overrides — из `deploy/bge-m3.env`. GPU image задаётся только после server inventory; пустой placeholder намеренно блокирует сборку.

## 3. Контракт GPU-сервиса

GPU backend должен сохранить внутренний HTTP-контракт платформы:

- `GET /health/ready` возвращает как минимум `model`, `native_dimension`, `output_dimension`;
- `POST /embed` принимает `inputs`, `normalize`, `truncate`, `input_type`;
- dense-векторы нормализованы, имеют ровно 1024 значения;
- query и passage обрабатываются согласованно;
- параллелизм ограничен, чтобы один большой batch не исчерпал VRAM.

Для первого A/B использовать только dense-выход BGE-M3. Sparse и ColBERT-выходы — отдельный следующий эксперимент: текущий PostgreSQL hybrid уже имеет FTS, и одновременная смена нескольких retrieval-компонентов не позволит понять источник изменения качества.

## 4. Предварительная проверка без записи

На изолированной БД после загрузки одного тестового документа:

```bash
python -m app.evaluation.embedding_reindex \
  --database-url "$DATABASE_URL" \
  --embedding-base-url http://embedding:8080 \
  --workspace-id "$WORKSPACE_ID" \
  --expected-model BAAI/bge-m3 \
  --expected-dimension 1024 \
  --dry-run
```

Успешный отчёт обязан содержать `database_dimension: 1024`, `embedding_dimension: 1024`, `dry_run: true` и `write_performed: false`. Затем корпус импортируется/переиндексируется полностью уже в изолированном пространстве.

## 5. Сравнение

Baseline и candidate запускаются на одинаковых документах и одном закрытом holdout. Сохраняются:

- Recall@1/5/10, MRR и nDCG;
- factual/citation coverage и корректность abstention;
- p50/p95 отдельно для embedding, retrieval и полного ответа;
- GPU VRAM, throughput, размер индекса и длительность полного reindex;
- список вопросов с регрессией, а не только средние значения.

Порядок сравнений:

1. MiniLM hybrid без reranker против BGE-M3 dense без reranker.
2. MiniLM hybrid против BGE-M3 hybrid с теми же RRF-весами.
3. Только для победившего embedding-профиля — отдельный reranker A/B.
4. Late chunking — отдельный индекс и отдельный эксперимент после выбора embedding baseline.

## 6. Критерий переключения

BGE-M3 не становится default только из-за более высокой размерности. Переключение допустимо, если candidate проходит существующие acceptance gates, не даёт критических регрессий по отдельным кейсам, укладывается в согласованный latency/VRAM budget и воспроизводимо пересобирается из чистого volume.

## Первичные источники

- [BAAI/bge-m3 model card](https://huggingface.co/BAAI/bge-m3) — размерность 1024, контекст до 8192 tokens и режимы dense/sparse/ColBERT.
- [FlagEmbedding: BGE-M3 inference](https://github.com/FlagOpen/FlagEmbedding/blob/master/research/BGE_M3/README.md) — официальный GPU-пример с `devices=['cuda:0']` и FP16.
