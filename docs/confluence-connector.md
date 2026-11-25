# Outbound Confluence Connector

Для текущего пилота используйте [пошаговое ручное обновление](pilot-manual-update.md):
оператор сам выполняет все серверные команды, затем запускает коннектор на ноутбуке.

Connector запускается на Ubuntu-машине, которая видит одновременно корпоративный
Confluence и RAG web endpoint. Он не открывает входящих портов и не является
универсальным HTTP-прокси: поддерживаются только `LIST_SPACES`, `LIST_CHILDREN`
`LIST_SPACE_PAGES` и `IMPORT_PAGES` для локально разрешённых пространств.

## Первый запуск на ноутбуке

### Текущий пилот: компьютер → сервер :15173, без OIDC

VM пока не требуется. Снимок из 298 страниц остаётся в существующем workspace;
новый импорт использует тот же механизм дедупликации по странице и содержимому.
Выключение ноутбука останавливает новые импорты, но не поиск по готовому индексу.

На сервере сохранить текущие имя Compose project, volumes и привязку порта.
В его `.env` для явного пилотного режима:

```dotenv
APP_ENV=server
AUTH_ENABLED=false
AUTH_ALLOW_UNAUTHENTICATED_PILOT=true
AUTH_LOCAL_SUBJECT=local-user
AUTH_LOCAL_TENANT=local
CONFLUENCE_CONNECTOR_ENABLED=true
```

`AUTH_LOCAL_SUBJECT/TENANT` должны совпадать с текущими значениями, иначе старые
workspace не будут видны. OIDC-токен для этого режима не нужен. Секрет чтения
Confluence нужен только на ноутбуке; это отдельная авторизация самого источника.
`CONFLUENCE_BASE_URL` на сервере оставить равным настоящему URL Confluence — он
проверяет ссылки источников даже при `CONFLUENCE_ENABLED=false`.

После резервной копии БД и доставки полного рабочего дерева (включая новые файлы
connector и миграцию 0010) обновить только API и web, из каталога проекта:

```bash
bash scripts/deploy/compose-server.sh build api web
bash scripts/deploy/compose-server.sh up -d --no-deps api
bash scripts/deploy/compose-server.sh logs --tail 60 api
curl --fail http://127.0.0.1:15173/api/v1/integrations/confluence/connector
bash scripts/deploy/compose-server.sh up -d --no-deps web
```

API применяет миграцию при старте. Продолжать после успешного GET, не только после
старта контейнера. `down`, удаление volumes и пересоздание соседних сервисов не нужны.
Если используется отдельный env-файл, передать его через `RAG_ENV_FILE`.

На ноутбуке можно использовать существующий корневой `.env` без копирования
секретов в новый файл. Из `apps/confluence_connector`:

```bash
uv sync --frozen
uv run python -m confluence_connector \
  --project-env ../../.env --rag-url http://192.0.2.60:15173 --allow-http --check
uv run python -m confluence_connector \
  --project-env ../../.env --rag-url http://192.0.2.60:15173 --allow-http --enroll-pilot
```

`--check` только читает API. `--enroll-pilot` создаёт код подключения автоматически
на сервере без OIDC и сохраняет отдельный машинный credential локально (права 600).
Повторный запуск использует сохранённый credential. Если код уже создан в UI,
его можно передать через `RAG_ENROLLMENT_TOKEN` и запускать без `--enroll-pilot`.
По умолчанию для каждого нормализованного RAG URL автоматически используется
отдельный файл `~/.config/rag-confluence-connector/credential-<sha256-url>.json`.
Указание другого сервера не перезаписывает credential старого сервера. Legacy-файл
`credential.json` копируется в scoped-файл только при совпадении сохранённого URL;
оригинал остаётся на месте. Файл без привязки к URL автоматически не переиспользуется.

Явно заданные `RAG_CREDENTIAL_FILE` и `--credentials-file` остаются строго привязаны
к сохранённому URL: ошибку «Saved credential belongs to another RAG_URL» в этом
случае нельзя обходить. Для нового сервера выберите новый путь либо уберите явную
настройку, чтобы использовать автоматическое разделение. CLI-флаг имеет приоритет.

### Новый демонстрационный сервер :15173 — все видимые пространства

Из `apps/confluence_connector`:

```bash
uv run python -m confluence_connector \
  --project-env ../../.env \
  --rag-url http://192.0.2.122:15173 --allow-http \
  --all-accessible-spaces --check
uv run python -m confluence_connector \
  --project-env ../../.env \
  --rag-url http://192.0.2.122:15173 --allow-http \
  --all-accessible-spaces --enroll-pilot
```

`--all-accessible-spaces` явно выбирает все пространства, которые Confluence REST
возвращает текущей настроенной учётной записи, с полной пагинацией списка.
Их реальные ключи становятся локальным allowlist и передаются при новой регистрации
на RAG; wildcard или обход прав доступа не используются. Авторизация — именно
PAT/basic/cookie из конфигурации connector, она может отличаться от учётки браузера.
Ограниченные страницы всё равно могут вернуть 403 и не будут импортированы.

Без флага сохраняется `CONFLUENCE_ALLOWED_SPACES` (по умолчанию `KBR`). Для полного
импорта страниц пространства используется пагинированный `LIST_SPACE_PAGES`,
который включает текущие страницы вне дерева homepage; раскрытие дерева в UI —
отдельная операция. Новые видимые spaces требуют повторного discovery при запуске.
Уже зарегистрированный credential не меняет серверный allowlist автоматически:
для его расширения нужна новая регистрация с отдельным private credential-файлом
или согласованное обновление/отзыв регистрации на сервере.

Явные настройки `.env.connector` и окружения имеют приоритет над fallback из
корневого `.env`. Если копируете пример `.env.connector`, замените настройки
`bearer`/token file на реально работающий способ авторизации, либо используйте
fallback без создания этого файла. При browser SSO см. cookie-режим ниже.

UI отправляет выбранные страницы пакетами не более 10. Временные сетевые ошибки,
429 и временные 5xx повторяются до трёх попыток. Страницы с окончательной ошибкой
остаются выбранными для повторного импорта; причина показывается в UI. Heartbeat
продлевает deadline активного задания с абсолютным пределом 30 минут.
Статус online означает связь с RAG; доступ Confluence проверяется `--check`
и операциями списка пространств/импорта.

### Ручная регистрация с отдельным PAT

1. Обновите RAG server и дождитесь применения Alembic migration `0010`.
2. В UI откройте вкладку Confluence, укажите `KBR` и создайте одноразовый код.
3. На ноутбуке подготовьте connector:

   ```bash
   cd apps/confluence_connector
   cp .env.connector.example .env.connector
   uv sync
   ```

4. Сохраните Confluence PAT локально, не добавляя его в проект:

   ```bash
   install -d -m 700 ~/.config/rag-confluence-connector
   read -rsp 'Confluence token: ' TOKEN
   printf '%s' "$TOKEN" > ~/.config/rag-confluence-connector/confluence-token
   unset TOKEN
   chmod 600 ~/.config/rag-confluence-connector/confluence-token
   ```

5. Заполните `.env.connector`: `RAG_ENROLLMENT_TOKEN`, `CONFLUENCE_AUTH_MODE`,
   `CONFLUENCE_ALLOWED_SPACES`. Для текущего пилотного HTTP endpoint явно оставьте
   `RAG_ALLOW_INSECURE_HTTP=true`; для постоянной установки нужен HTTPS.
6. Запустите:

   ```bash
   uv run python -m confluence_connector
   ```

После enrollment одноразовый код больше не используется. RAG credential хранится
в отдельном для текущего RAG URL файле с правами `600` (или в явно выбранном пути).

## Avanpost browser session

Ответ `401` без авторизации подтверждает сетевую доступность Confluence, но не
передаёт браузерную SSO-сессию connector. Предпочтителен PAT технической учётной
записи. Для временного теста поддерживается `CONFLUENCE_AUTH_MODE=cookie`: файл
`CONFLUENCE_COOKIE_FILE` должен содержать только значение HTTP-заголовка `Cookie`
и иметь права `600`. Cookie истекает, поэтому этот режим не подходит для сервиса.

## Проверка

В UI статус должен стать `Connector online`. Затем:

1. загрузите список пространств;
2. раскройте KBR и дерево страниц;
3. выберите страницу `202211666` либо добавьте этот ID вручную;
4. импортируйте страницу и дождитесь статуса документа `READY`;
5. повторите импорт — неизменившаяся страница должна быть пропущена.

## Production

- используется HTTPS через согласованный вход (это может быть тот же `15173`);
- connector использует service account с минимальным набором spaces;
- RAG и локальный connector применяют одинаковые allowlist; opt-in
  `--all-accessible-spaces` не заменяет Confluence ACL и не реализует их перенос в RAG;
- credential можно отозвать в tenant scope;
- PostgreSQL хранит authoritative jobs, Redis используется только как wake-up;
- lease, heartbeat, retry и idempotent import переживают краткие обрывы связи.
