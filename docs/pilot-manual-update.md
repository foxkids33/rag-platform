# Ручное обновление пилота: ноутбук → Confluence → RAG

Все команды на сервере выполняет оператор. Рабочий снимок 298 страниц остаётся
на месте. Сейчас обновляются только API и web, используется прежний порт 15173.
VM, OIDC и смена рабочей embedding-модели в этот этап не входят.

## 1. На ноутбуке: подготовить исходники

Из корня проекта:

```bash
bash scripts/deploy/package-server.sh
```

Команда выводит путь `.storage/releases/rag-pilot-XXXXXXXX.tar.gz` и SHA-256.
Включены незакоммиченные исходники и новые файлы коннектора/миграции. Настоящие
`.env`, credentials, виртуальные окружения, модели и снимки не включены.
Передайте архив на сервер своим привычным способом. Не заменяйте серверный
`.env` локальным. Git pull сам по себе не доставит текущие новые файлы.

## 2. На сервере: установить точный рабочий проект

```bash
docker ps --filter publish=15173 --format 'table {{.Names}}\t{{.Label "com.docker.compose.project"}}\t{{.Ports}}'
```

Перейдите в существующий каталог приложения (например `/opt/rag-kbr-v01`, если
это действительно ваш каталог). Сохраните **фактическое** имя проекта из вывода
выше в `COMPOSE_PROJECT_NAME` текущей shell-сессии. Пример только для совпадающего
имени:

```bash
export COMPOSE_PROJECT_NAME=rag-kbr-v01
bash scripts/deploy/compose-server.sh ps
```

Список должен содержать уже работающие RAG-контейнеры. Если список пуст или это
другие контейнеры — сначала исправьте каталог, имя проекта или `RAG_ENV_FILE`.
Занятый вашим web порт 15173 при обновлении нормален: новый порт не выбирайте.

До распаковки сделайте резервную копию БД и текущего приложения:

```bash
umask 077
mkdir -p .storage
pilot_backup_dir="$(mktemp -d "$PWD/.storage/pre-connector-XXXXXXXX")"
cp .env "$pilot_backup_dir/server.env"
bash scripts/deploy/compose-server.sh exec -T postgres \
  sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' \
  > "$pilot_backup_dir/database.dump"
test -s "$pilot_backup_dir/database.dump"
tar --exclude=.venv --exclude=node_modules --exclude=dist --exclude=__pycache__ \
  -czf "$pilot_backup_dir/source.tar.gz" apps deploy scripts docs
for service in api web; do
  container_id="$(bash scripts/deploy/compose-server.sh ps -q "$service")"
  docker inspect --format '{{.Image}}' "$container_id" > "$pilot_backup_dir/$service.image-id"
done
```

Этот dump не заменяет backup MinIO для аварийного восстановления; при обновлении
API/web его volume не пересоздаётся. Сохраните обычную резервную копию хранилища
по вашей процедуре. При любой ошибке backup не переходите к обновлению.

Проверьте SHA-256 переданного архива, сравните с выводом ноутбука. Распакуйте его
в этот же каталог приложения, указав точный путь переданного файла:

```bash
sha256sum /tmp/rag-pilot-XXXXXXXX.tar.gz
tar -xzf /tmp/rag-pilot-XXXXXXXX.tar.gz -C "$PWD"
```

## 3. Серверный env и обновление API/web

В существующем серверном `.env` задайте:

```dotenv
APP_ENV=server
AUTH_ENABLED=false
AUTH_ALLOW_UNAUTHENTICATED_PILOT=true
CONFLUENCE_CONNECTOR_ENABLED=true
```

Сохраните прежние `AUTH_LOCAL_SUBJECT`, `AUTH_LOCAL_TENANT`, `COMPOSE_PROJECT_NAME`,
привязку `RAG_BIND_HOST`, `RAG_WEB_HOST_PORT=15173`, параметры БД/MinIO и модели.
Если subject/tenant ранее отсутствовали, их прежние defaults — `local-user` и
`local`. `CONFLUENCE_BASE_URL` должен остаться настоящим URL источника для ссылок.
Настройки GPU/BGE здесь пока не меняйте.

Без OIDC интерфейс и административные действия доступны каждому, кто может
достучаться до сервиса. Такой пилот допустим только в уже ограниченной доверенной
сети; не публикуйте его в интернет. `--allow-http` ниже явно разрешает передачу
данных и машинного credential без TLS, поэтому маршрут ноутбук → сервер тоже
должен быть доверенным.

```bash
bash scripts/deploy/compose-server.sh config --quiet
bash scripts/deploy/compose-server.sh build api web
bash scripts/deploy/compose-server.sh up -d --no-deps api
bash scripts/deploy/compose-server.sh logs --tail 80 api
bash scripts/deploy/compose-server.sh exec -T api python -c \
  'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:8000/api/v1/integrations/confluence/connector", timeout=10).read().decode())'
```

API применяет миграцию `0010_outbound_connector` при запуске. Дождитесь успешного
ответа с `enabled: true`; при ещё не готовом API повторите проверку после старта.
Проверяем внутри контейнера: старый nginx может сохранять прежний IP API до
обновления web. При ошибке миграции или HTTP 404 остановитесь и сохраните
последние логи API. Полный вывод `config` и `.env` никому не пересылайте.

Затем обновите web:

```bash
bash scripts/deploy/compose-server.sh up -d --no-deps web
bash scripts/deploy/compose-server.sh ps
curl --fail --max-time 10 http://127.0.0.1:15173/api/v1/integrations/confluence/connector
curl --fail --max-time 10 http://127.0.0.1:15173/api/v1/system/info
```

Если порт опубликован только на конкретном интерфейсе сервера, используйте его
адрес вместо `127.0.0.1`. Проверьте в браузере старый workspace, документы и один прежний вопрос.
`down`, `down -v`, очистка Docker, перезапуск Docker/NVIDIA/vLLM не требуются.

Для отката приложения используйте сохранённые исходники и image ID API/web:
верните старые исходники, присвойте сохранённым image ID исходные теги сборки
вашего Compose-проекта и пересоздайте только API/web с `--no-build --no-deps`.
Миграция 0010 добавляет отдельные таблицы; удалять их для возврата старого API
не требуется. Существующую БД из dump без отдельной необходимости не заменяйте.

## 4. На ноутбуке: запустить коннектор

Из корня проекта:

```bash
cd apps/confluence_connector
uv sync --frozen
uv run python -m confluence_connector \
  --project-env ../../.env --rag-url http://192.0.2.60:15173 --allow-http --check
```

Ожидаются три проверки: `RAG HTTP: OK`, `Confluence REST: OK` с ненулевым числом
spaces, `RAG connector API: OK`. Существующий локальный `.env` ранее прошёл
проверку Confluence; повтор нужен после обновления сервера.

```bash
uv run python -m confluence_connector \
  --project-env ../../.env --rag-url http://192.0.2.60:15173 --allow-http --enroll-pilot
```

Оставьте процесс работающим. Он не открывает входящих портов. Первый запуск
регистрирует машинный credential на сервере без OIDC и сохраняет его на ноутбуке.
Повторный использует тот же файл. Если существует `.env.connector`, его явные
настройки имеют приоритет над fallback из корневого `.env`.

## 5. Проверить новый импорт

1. В существующем workspace открыть Confluence, загрузить spaces и дерево KBR.
2. Выбрать одну страницу. Для проверки именно новой записи выбрать ещё не
   импортированную страницу; уже имеющаяся неизменённая страница будет пропущена.
3. Дождаться сначала постановки в очередь, затем `READY` в списке документов.
4. Задать вопрос, ответ на который есть на этой странице, проверить цитату/ссылку.
5. Повторить импорт неизменённой страницы: дубликат не создаётся.
6. Остановить коннектор Ctrl+C: поиск по старым документам продолжает работать.
   Затем запустить той же командой и проверить восстановление статуса online.

Для диагностики достаточно HTTP-статуса, статуса операции и короткого фрагмента
лога без credentials/корпоративного текста. Не передавайте cookie и токены в чат.

## 6. После успешного импорта: подготовить BGE-M3

На сервере только прочитать состояние GPU/runtime:

```bash
nvidia-smi --query-gpu=index,uuid,name,memory.total,memory.used,memory.free,driver_version --format=csv
nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
docker compose version
docker info --format '{{json .Runtimes}}'
```

По этому выводу выбираются CUDA-образ и доступная карта. Дальнейшие команды —
в [BGE-M3 A/B](bge-m3-server-ab.md). Стенд использует отдельные volumes и БД,
не публикует порты и не меняет рабочую `vector(384)`. Пока эти сведения не
получены, не подставляйте случайный CUDA-образ/GPU ID.
