# Первый внутренний релиз на BGE-M3

## Границы перехода

BGE-M3 заменяет embedding-модель, а не текущую LLM. Используется уже готовый
проект `rag-kbr-bge-ab`, workspace `b15f3afe-55c0-4fa5-9c6c-1fe17779cade`, 298 страниц.
Имя Compose-проекта **не менять**: оно определяет существующие volumes.
Рабочий `rag-kbr-v01` с MiniLM сохраняется; при переключении останавливается
только его web. Векторы 384 и 1024 не смешиваются, переиндексация не нужна.

Этот путь допустим **только после согласования** использования отдельной BGE-базы.
Документы и диалоги рабочего MiniLM, которых нет в BGE, не мигрируют автоматически.
Если их обязательно видеть в релизе, остановиться до шага 6 и отдельно спланировать
миграцию. Существующий Confluence-коннектор также привязан к старой БД: для новых
импортов понадобится отдельная регистрация в BGE; старый credential не копировать.

Релиз предназначен для доверенного внутреннего доступа. Сохраняются текущие
настройки auth и сетевой привязки. При `AUTH_ENABLED=false` не открывать сервис
в интернет: доступ должен быть ограничен корпоративной сетью/firewall. Просмотр
готовых 298 страниц не подтверждает готовность нового импорта или разграничения
прав. Показ команд — цитирование, не разрешение выполнять их на рабочем кластере.

## 1. Ноутбук: собрать и передать пакет

```bash
cd /path/to/PycharmProjects/rag-platfrom/rag-platform-bootstrap
bash scripts/deploy/package-bge-release.sh
```

Скопировать напечатанный путь архива, подставив его ниже:

```bash
scp .storage/releases/bge-release-XXXXXXXX.tar.gz \
  root@192.0.2.60:/root/bge-release-update.tar.gz
```

Пакет включает незакоммиченные изменения только явно перечисленных файлов.
Базовый Compose, `.env` и `deploy/bge-m3.env` не меняются.

## 2. Сервер: проверить проекты и сохранить откат

Все серверные действия выполняет оператор, после завершения активных генераций.
Команды далее — в одной Bash/SSH-сессии. При ошибке не продолжать.

```bash
cd /root/rag-kbr-v01
export COMPOSE_PROJECT_NAME=rag-kbr-v01
bash scripts/deploy/compose-server.sh ps
bash scripts/deploy/compose-bge.sh ps
```

Проверить, что это существующие проекты `rag-kbr-v01` и `rag-kbr-bge-ab`.
Не применять `down`, `down -v`, очистку volumes или смену размерности рабочей БД.

```bash
release_backup=$(mktemp -d /root/rag-bge-release-backup.XXXXXX)
chmod 700 "$release_backup"
tar -czf "$release_backup/sources-before.tar.gz" \
  apps/api/app/services/context_builder.py \
  apps/api/app/services/llm.py \
  apps/api/app/api/routes/answer.py \
  apps/api/app/evaluation/models.py \
  apps/api/app/evaluation/dataset.py \
  apps/api/app/evaluation/metrics.py \
  apps/web/src/App.tsx apps/web/src/styles.css \
  scripts/deploy/compose-bge.sh
docker inspect --format '{{.Name}} {{.Image}} {{.Config.Image}}' \
  rag-kbr-v01-api-1 rag-kbr-v01-web-1 rag-kbr-bge-ab-api-1 \
  > "$release_backup/images-before.txt"

bash scripts/deploy/compose-bge.sh exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$release_backup/bge-database.dump"
test -s "$release_backup/bge-database.dump"
echo "Backup: $release_backup"
tar -tzf /root/bge-release-update.tar.gz
```

Проверить список архива: только исходники, один release-overlay, split и инструкция.
Бэкап БД содержит корпоративные данные: не прикладывать его к отчёту.
Для существующей BGE-базы проверьте также наличие штатной резервной копии файлов
MinIO; эти файлы данный переход не меняет. Старые image ID не удалять.

## 3. Установить патч только в BGE, ещё без переключения UI

```bash
tar -xzf /root/bge-release-update.tar.gz -C /root/rag-kbr-v01
bash scripts/deploy/compose-bge.sh --release build api web
```

После успешной сборки:

```bash
bash scripts/deploy/compose-bge.sh --release up -d --no-deps --no-build --force-recreate api
bash scripts/deploy/compose-bge.sh --release ps
bash scripts/deploy/compose-bge.sh --release logs --tail 80 api
```

Дождаться healthy. MiniLM API/web пока продолжают работать. Release-overlay
выключает автоматическую инициализацию размерности: используются готовые векторы.
Не пересобирать и не перезапускать embedding, worker, postgres, vLLM.

```bash
bash scripts/deploy/compose-bge.sh --release exec -T api \
  python -m app.operations.initialize_embedding_schema --dimension 1024 --dry-run

bash scripts/deploy/compose-bge.sh --release exec -T api python - <<'PY'
import json
from urllib.request import urlopen
from app.core.config import settings
for url in ["http://localhost:8000/api/v1/system/info",
            "http://localhost:8000/api/v1/workspaces",
            settings.embedding_base_url.rstrip("/") + "/health/ready"]:
    with urlopen(url, timeout=30) as response:
        print(json.dumps(json.load(response), ensure_ascii=False))
PY
```

Ожидается: `database_dimension=1024`, `change_required=false`,
`write_performed=false`; модель API и embedding — `BAAI/bge-m3`, выходная
размерность 1024; целевой workspace содержит 298 searchable documents.
При другом результате остановиться. Эти HTTP-команды рассчитаны на текущий
local-auth пилот; при OIDC использовать обычную авторизацию, не отключать её.

## 4. Сначала четыре проблемных случая

```bash
release_run="bge-release-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "evaluation/results/$release_run"
bash scripts/deploy/compose-bge.sh --release exec -T api \
  python -m app.evaluation.runner \
  --base-url http://localhost:8000 \
  --workspace-id b15f3afe-55c0-4fa5-9c6c-1fe17779cade \
  --dataset /evaluation/results/kbr.v1.prepared-v2.jsonl \
  --case-ids /evaluation/splits/kbr-release-focus.txt \
  --mode hybrid --limit 10 --no-rerank --answers \
  --answer-max-tokens 4096 --answer-temperature 0 --fail-fast \
  --output "/evaluation/results/$release_run/focus.json"
echo "$release_run"
```

Проверить ответы: 003 — один точный блок четырёх команд; 016 — Uptime и тестовые
запросы к корзине; 026 — включённое для корзины сжатие и ZSTD; 027 — все три
ограничения Round-Robin. `ok` означает только выполнение, не правильность ответа.
Если есть регрессия — прислать focus.json, UI пока не переключать.

## 5. Полный прогон и автоматический допуск

```bash
bash scripts/deploy/compose-bge.sh --release exec -T api \
  python -m app.evaluation.runner \
  --base-url http://localhost:8000 \
  --workspace-id b15f3afe-55c0-4fa5-9c6c-1fe17779cade \
  --dataset /evaluation/results/kbr.v1.prepared-v2.jsonl \
  --mode hybrid --limit 10 --no-rerank --answers \
  --answer-max-tokens 4096 --answer-temperature 0 --fail-fast \
  --output "/evaluation/results/$release_run/full.json"
```

Скачать каталог с ноутбука, заменив `bge-release-ДАТА` напечатанным именем:

```bash
scp -r root@192.0.2.60:/root/rag-kbr-v01/evaluation/results/bge-release-ДАТА \
  evaluation/results/
node scripts/evaluation/check-bge-release.mjs \
  evaluation/results/bge-release-ДАТА/full.json
```

Нужен `PASS`. Проверка требует 36 успешных случаев, прежний v2, правильные
отказы, отсутствие обрезок, сохранение Recall@5, точных значений и блоков команд,
полноту ответов и gold-контекста 016/026/027. Старые отчёты и v3 не подставлять.
Это проверка разработческого набора, не доказательство общей production-готовности.

## 6. Переключить UI после PASS и согласования базы

Сервер, тот же каталог и `COMPOSE_PROJECT_NAME=rag-kbr-v01`:

```bash
bash scripts/deploy/compose-server.sh stop web
bash scripts/deploy/compose-bge.sh --release up -d --no-deps --no-build web
bash scripts/deploy/compose-bge.sh --release ps
curl --fail --show-error --max-time 10 http://127.0.0.1:15173/api/v1/system/info
curl --fail --show-error --max-time 10 http://127.0.0.1:15173/api/v1/workspaces
```

При ошибке web сразу выполнить откат ниже. Должен быть BGE-M3/1024 и BGE-workspace,
а не старый MiniLM-workspace. Порт прежний, остальные сервисы остаются без публикации.
**После перехода для обслуживания BGE использовать `compose-bge.sh --release`.**
Обычный wrapper без флага относится к изолированному A/B-режиму без UI-порта.

Обновить браузер с очисткой кэша; выбрать BGE-workspace и начать новый диалог.
Повторить четыре вопроса из focus; проверить источники, лимит ответа, сохранение
диалога после перезагрузки. UI не идентичен eval: у него история диалога и свой
бюджет ответа. Отдельно убедиться, что старые localStorage-ID диалогов не мешают
выбрать новую область. Проверьте сохранность BGE-диалога повторным открытием.

## Откат UI

```bash
bash scripts/deploy/compose-bge.sh --release stop web
bash scripts/deploy/compose-server.sh start web
curl --fail --show-error --max-time 10 http://127.0.0.1:15173/api/v1/system/info
```

Вернётся прежний MiniLM UI/API с прежними данными; их контейнеры не пересоздавались.
Новые диалоги BGE остаются в BGE-базе, в MiniLM они автоматически не появляются.
Откат UI не требует восстановления dump, удаления БД, volumes или GPU-модели.

## Что прислать

- `focus.json` и `full.json`, результат `check-bge-release.mjs`.
- Вывод проверки размерности и `/system/info` после переключения.
- Подтверждение, что выбран BGE-workspace, новый диалог сохраняется, ссылки открываются.
- При сбое — короткий лог соответствующего API/web без credentials.

Никаких `.env`, дампов БД или токенов в сообщениях. Старые результаты сохранить.
