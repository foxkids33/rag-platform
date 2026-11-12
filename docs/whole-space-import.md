# Импорт всего Confluence space

В UI: Confluence → Показать пространства → «Импортировать весь space» рядом
с нужным пространством → подтвердить количество страниц.

Коннектор запрашивает все доступные ему текущие страницы space, а не только
потомков homepage. Метаданные получаются страницами до 100 записей; HTML
передаётся в API пакетами по 10 и поступает в существующую очередь индексации.
Неизменённые документы пропускаются; ошибки и непереданные страницы остаются
в выборе. Общий обход не ограничен прежними 1000 результатами дерева.
Вложения, черновики и недоступные учётной записи страницы не импортируются.

До окончания передачи держать раздел открытым и коннектор запущенным. Это пока
не независимая серверная задача: уход из раздела прекращает отправку следующих
пакетов, но уже принятые сервером задания продолжают работу. Для продолжения
можно заново импортировать space: неизменённые страницы будут пропущены.
Готовность индексации — статус `READY`/«Готов», а не завершение передачи.

## Деплой

На компьютере из корня репозитория:

```bash
rsync -avR --exclude='__pycache__/' --exclude='.env*' \
  apps/api/app/ apps/api/alembic/ apps/web/src/ \
  root@192.0.2.60:/root/rag-kbr-v01/
```

На сервере:

```bash
cd /root/rag-kbr-v01
export COMPOSE_PROJECT_NAME=rag-kbr-v01
bash scripts/deploy/compose-server.sh build api web &&
bash scripts/deploy/compose-server.sh up -d --no-deps --no-build api &&
bash scripts/deploy/compose-server.sh up -d --no-deps --no-build --force-recreate web
```

Миграция 0011 автоматически расширяет допустимые типы `connector_jobs`, не меняя
документы и векторы. Прямой downgrade невозможен, пока существуют задания нового
типа: миграция не удаляет историю ради отката. Worker, хранилища и BGE не обновляются.

На компьютере остановить старый коннектор Ctrl+C и запустить:

```bash
cd ~/PycharmProjects/rag-platfrom/rag-platform-bootstrap/apps/confluence_connector
uv run python -m confluence_connector \
  --project-env ../../.env --rag-url http://192.0.2.60:15173 \
  --allow-http --enroll-pilot
```

Сохранённый credential используется повторно. Новый capability `LIST_SPACE_PAGES`
передаётся через heartbeat; до его получения кнопка импорта space отключена.
