# Серверный коннектор с Cisco Secure Client

VPN, исходящий коннектор и TLS-транспорт работают в сетевом пространстве контейнера VPN. Вход в Cisco с MFA проходит в серверной графической сессии. Для доступа к её сокету `/run/rag-confluence-vpn/display/login.sock` оператор использует SSH с правами владельца. После входа окно можно закрыть. Срок сеанса задаёт VPN-шлюз.

Для существующей установки сохраните имя Docker-сети проекта и текущие теги образов. Задайте адреса своего шлюза и wiki по [шаблону настроек](settings.env.example) в `/etc/rag-confluence-vpn.env`. Оба systemd-сервиса читают этот файл. Учётные данные зарегистрированного коннектора, его файл `worker.env`, разрешённые пространства и доверенный CA находятся в закрытом каталоге хоста `/var/lib/rag-confluence-connector`. [Регистрация коннектора](../../docs/confluence-connector.md)

Для новой установки системному администратору нужны Linux с Docker, systemd, TUN, nsenter, отдельная сеть RAG и образ с лицензированным Cisco Secure Client для Linux в `/opt/cisco/secureclient`. Базовый образ должен включать пользователя `vpnuser` с UID/GID 1000, Python 3, X11-утилиты, Openbox и D-Bus. Dockerfile добавляет TigerVNC и xauth. Для транспорта нужен отдельный каталог `/var/lib/rag-confluence-transport`, доступный UID 1000 с режимом 0700.

```bash
docker build --build-arg CISCO_BASE_IMAGE=your-cisco-client:gui -t rag-confluence-vpn:local deploy/confluence-vpn
docker build -t rag-confluence-connector:local apps/confluence_connector
sudo install -d /usr/local/libexec/rag-confluence-vpn
sudo install -m 0644 deploy/confluence-vpn/*.py deploy/confluence-vpn/worker.compose.yaml /usr/local/libexec/rag-confluence-vpn/
sudo install -m 0644 deploy/confluence-vpn/*.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rag-confluence-vpn.service rag-confluence-worker.service
```

Статус туннеля и время до его отключения показывает `sudo docker exec rag-confluence-vpn /opt/cisco/secureclient/bin/vpn stats`. На уровне приложения проверяйте «Confluence → Показать пространства» и открытие дерева страниц. Такой запрос проходит через API RAG и зарегистрированный коннектор до самой wiki.

## Личный токен Confluence в RAG

В установке с `rag-delivery` API использует закрытый Unix-сокет транспорта и проверяет сертификат корпоративного Confluence по имени. Сокет доступен UID 1000, под которым работает API. Для сеансов сотрудников нужны парольная авторизация RAG, HTTPS и защищённые cookies. Администратор один раз выполняет из каталога выпуска следующую команду, подставив HTTPS-адрес своей wiki.

```bash
sudo python3 deploy/delivery/enable_personal_confluence.py --base-url https://wiki.example.org
```

Если Confluence использует частный CA, добавьте `--ca-file /абсолютный/путь/confluence-ca.pem`. Файл должен содержать публичные сертификаты, доступные на чтение пользователю API. Скрипт добавляет настройки и read-only mounts в `/etc/rag-delivery/secure.json`, сохраняет исходную конфигурацию в `secure.before-personal.json` рядом и создаёт постоянный ключ `confluence-personal.key` с режимом 0440 и группой API. Повторный запуск использует тот же ключ. Включите его в закрытую резервную копию конфигурации.

Следующий выпуск API пересоздаст контейнер с этими настройками. При проверке войдите в RAG как обычный сотрудник, подключите его личный Confluence PAT и откройте «Показать пространства». После обрыва VPN запросы к wiki возобновятся, когда оператор восстановит туннель. В пределах того же действующего сеанса RAG сохранённый токен доступен после перезапуска API.
