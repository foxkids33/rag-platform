# Reviewer со второго компьютера: корпоративный сервер, без ngrok

Выбран внутренний доступ через `192.0.2.122`. Локальный Confluence
остаётся на ноутбуке. `localhost` на другом компьютере означает другой
компьютер, поэтому непосредственно Reviewer на ноутбуке там не откроется.

```text
Другой компьютер → HTTPS 192.0.2.122:15174 → собственный nginx gateway
  /confluence → server 127.0.0.1:23991 → reverse SSH → ноутбук 127.0.0.1:23990
  /synchrony  → server 127.0.0.1:23992 → тот же SSH → ноутбук 127.0.0.1:8091
```

Вход двойной: отдельный случайный gateway-пароль, затем штатный Confluence.
В clone шесть пространств, два не QAEVAL; он не считается исключительно
учебным. Доступ временный, только доверенному демонстратору, без стороннего
публичного туннеля. Ngrok-helper в этом сценарии **не запускать**.

Во время этого показа браузер даже на ноутбуке открывает
`https://192.0.2.122:15174/confluence`. Порт `127.0.0.1:23990`
остаётся HTTP **backend** для SSH relay, но Tomcat настроен понимать запрос
как внешний HTTPS. Прямой HTTP login через 23990 в этой временной фазе
не является поддержанным пользовательским маршрутом; mismatch origin
при таком входе не исправлять отключением CSRF.

## Подготовка на ноутбуке

Из корня RAG-репозитория:

```bash
node scripts/demo/reviewer-internal-access.mjs self-test
node scripts/demo/reviewer-internal-access.mjs prepare
node scripts/demo/reviewer-internal-access.mjs status
```

Это только подготовка файлов, не запуск SSH, контейнеров или модели.
Игнорируемый каталог `.storage/reviewer-internal-access/` имеет 0700.
Ключ, hash и локальные credentials/config — 0600; публичный `tls.crt` — 0644.

- `credentials.json` — gateway-login и случайный 32-символьный пароль.
  Остаётся только на ноутбуке: **не копировать на сервер** и не показывать на слайдах.
- `htpasswd` — salted apr1 hash, не исходный пароль.
- `tls.key` / `tls.crt` — отдельный self-signed RSA3072 leaf-сертификат
  на два дня с IP SAN `192.0.2.122`.
- `nginx.conf` — два собственных loopback upstream на сервере:
  Confluence23991 и Synchrony23992, без recorder или модели.
- `status.json` — безопасные metadata и SHA-256 fingerprint сертификата.

OpenSSL получает gateway-пароль через stdin, не через argv. SSH-пароль не
сохраняется, SSH-ключи не создаются. Повторная подготовка сохраняет существующие
секреты и сертификат. Неполный комплект или недостаточный срок сертификата
не удаляется и не перегенерируется молча.

## Собственный gateway на сервере

Перед новым запуском убедиться, что `15174`, `23991` и `23992` свободны. Использовать только
`/home/intern/ZMostovoy/reviewer-access` и контейнер `reviewer-internal-access`.
RAG, общая модель и чужие контейнеры не меняются.

На сервер переносить ровно четыре файла: `nginx.conf`, `htpasswd`,
`tls.crt`, `tls.key`. Хостовый каталог — 0700. **Ключ на сервере root:root,
0600**: root-master nginx без CAP_DAC_OVERRIDE должен владеть ключом.
Hash/config/cert на сервере — 0644; raw gateway-password туда не передаётся.
Hash читается worker nginx через individual read-only bind mount; private
хостовый каталог не становится общедоступным.

Текущий gateway запущен собственным transient systemd-unit
`reviewer-internal-proxy-20261009.service`: `RuntimeMaxSec=30h`, `Restart=no`.
Unit запускает **attached** `docker run --rm` с именем
`reviewer-internal-access`, без `-d`; `ExecStop` останавливает только этот
контейнер. Поэтому TTL завершает сам gateway, а не только shell запуска.
Не заменять этот запуск detached-контейнером без срока жизни.

Используется уже проверенный существующий RAG web image, без rebuild:
host network, read-only root filesystem, `no-new-privileges`,
`--cap-drop=ALL --cap-add=CHOWN --cap-add=SETUID --cap-add=SETGID`;
tmpfs `/var/cache/nginx` (32 MiB) и `/var/run` (4 MiB), оба
`nosuid,nodev,noexec`. Четыре файла смонтированы по отдельности read-only
в `/etc/nginx/reviewer-access/`; каталоги RAG и Confluence не монтируются.

`nginx -t` прошёл до запуска. Worker — обычный `nginx`; SETUID/SETGID нужны master для
понижения прав, CHOWN — для своих временных каталогов на tmpfs.
Остальные capabilities удалены, root filesystem read-only.
Host network нужен для loopback upstream. Сам nginx слушает только
`192.0.2.122:15174`, не `0.0.0.0`.

Read-only проверка на сервере:

```bash
systemctl show reviewer-internal-proxy-20261009.service \
  -p ActiveState -p RuntimeMaxUSec -p Restart -p ExecStop
docker inspect reviewer-internal-access \
  --format '{{.State.Status}} {{.HostConfig.ReadonlyRootfs}} {{.HostConfig.NetworkMode}}'
```

## Обратный SSH с ноутбука

Соединение инициирует ноутбук к серверу. SSH-пароль введён интерактивно и
не сохранён. Оба обратных порта на сервере привязаны только к
`127.0.0.1:23991` и `127.0.0.1:23992`. Не открывать их на `0.0.0.0`
и не менять `GatewayPorts`.

Текущий master использует короткий socket
`/tmp/reviewer-122-demo30h.s9fDLr/control` в собственном каталоге 0700;
полный путь внутри репозитория превышал лимит Unix socket. Актуальный
socket сохранён в `.storage/reviewer-internal-access/ssh-runtime.json`.
Далее команды из корня репозитория читают только этот безопасный metadata:

```bash
REVIEWER_SOCKET="$(node -p "JSON.parse(require('node:fs').readFileSync('.storage/reviewer-internal-access/ssh-runtime.json', 'utf8')).control_socket")"
ssh -S "$REVIEWER_SOCKET" -O check intern@192.0.2.122
```

Использованы `StrictHostKeyChecking=yes` и отдельный `known-hosts` с
ранее сверенным ключом сервера: неизвестный host key не принимается
автоматически. Остальные параметры текущего master: `ControlPersist=no`,
`ExitOnForwardFailure=yes`, `ServerAliveInterval=30`, `ServerAliveCountMax=3`.
Recorder28001, RAG15173 и LLM через этот relay не публикуются.
Synchrony доступен только как необходимый transport редактора по
`/synchrony` того же HTTPS15174 gateway с той же обязательной авторизацией.
Отдельный public port8091 на сервере не открывается.

## Synchrony без отдельного публичного порта на сервере

Remote login сам по себе не гарантирует рабочий редактор. В первоначальном
clone `synchrony-proxy` webapp отсутствовал, и `/synchrony/heartbeat` и
`/confluence/synchrony-proxy/heartbeat` возвращали404; поэтому native editor
не завершал загрузку. При этом собственный Synchrony на ноутбуке был жив:
`http://127.0.0.1:8091/synchrony/heartbeat` →200 `OK`.

Использована [официальная схема direct Synchrony через reverse proxy](https://confluence.atlassian.com/conf92/possible-confluence-and-synchrony-configurations-1477577339.html):
в уже принадлежащий демонстрации SSH master добавлен reverse forward
`127.0.0.1:23992:127.0.0.1:8091`. `nginx location /synchrony` отправляет
запрос на `http://127.0.0.1:23992` без URI suffix, сохраняя
`/synchrony/...`. Gateway BasicAuth наследуется от server, upstream
Authorization удаляется; WebSocket Upgrade/Connection передаются.
Collaboration не выключалась. Heartbeat и путь через защищённый gateway
работают; это не полная проверка всех WebSocket, collaborative autosave
и сохранения черновиков Confluence.

Оба forward принадлежат одному master и закрываются одним 30-часовым
expiry timer или его явной остановкой. Metadata
`ssh-runtime.json` содержит `synchrony_remote_bind` и
`synchrony_local_target`; gateway ограничен своим transient unit TTL.
Для завершения всего relay не нужен отдельный процесс или общий `pkill`.

Ownership Synchrony проверен read-only: PID441000, parent440452,
cgroup `confluence-qaeval-20261007.service`, пути собственного stand.
Это зафиксированные PID проверки, не цели для будущего `kill`.
Существующий dev Synchrony на ноутбуке слушает `*:8091`, а не только
loopback; обращение SSH к127.0.0.1 не меняет этот bind. Поэтому фраза
«не публикуем8091» означает **не добавляем отдельный публичный порт
на сервере**, а не полную production-hardening ноутбука.
Этот стенд — временная демонстрация с известными dev ограничениями.

Активный одноразовый таймер
`reviewer-reverse-ssh-expiry-20261009-demo30h.timer` закрывает именно этот SSH
master и оба его forward через 30 часов. Проверить на ноутбуке:

```bash
systemctl --user show reviewer-reverse-ssh-expiry-20261009-demo30h.timer \
  -p ActiveState -p NextElapseUSecMonotonic
systemctl --user show reviewer-reverse-ssh-expiry-20261009-demo30h.service \
  -p ExecStart
```

`ControlPersist=30h` не гарантирует остановку активного туннеля — поэтому
используется отдельный timer; на сервере gateway дополнительно ограничен
собственным `RuntimeMaxSec=30h`. Продление — отдельное явное действие
оператора, не автоматическое. При новом запуске использовать новый короткий
private socket directory, сохранить новый metadata и сверить ExecStart
таймера. Не оставлять старый timer для нового master с тем же socket.
Автозапуск после reboot и automatic restart не включены.

Восстановленная 9 октября сессия имеет следующие предельные сроки (МСК):
reverse SSH timer — **10 октября 15:59:00**, собственный gateway после
точечного restart — **10 октября 15:55:05**. Для Reviewer создан отдельный
background master `ssh -M -N -f` с `ControlPersist=no`: 8-часового idle-timeout
больше нет. Абсолютный expiry `2026-10-10 12:59:00 UTC` закроет именно его.
Оба прежних loopback forward перенесены на него; предыдущий SSH master
оставлен без Reviewer forward для публикации RAG. Доступ закончится по более
раннему сроку gateway. Таймер — максимальная продолжительность, а не гарантия
непрерывной доступности при потере VPN или выключении ноутбука.
Сертификат и оба пароля не менялись.

Ноутбук должен оставаться включённым, подключённым и не засыпать.
При потере SSH gateway сохраняет авторизацию и возвращает upstream error,
а не превращается в открытый доступ.

## Проверка перед выступлением

Адрес: `https://192.0.2.122:15174/confluence`.

9 октября 2026, 09:56 МСК восстановлены прежние own local clone и recorder,
две loopback reverse SSH связи и защищённый gateway. Confluence `RUNNING`,
Synchrony heartbeat200. Повторная HTTP-проверка со строгим TLS подтвердила
401 без gateway-пароля / с неверным паролем / для прямой ссылки,
login200 с gateway-паролем и Synchrony200 `OK` после авторизации.
Настоящий браузер с точным SPKI pin прошёл Confluence login и загрузил
native editor собственной статьи5636125: кнопка «Проверить» доступна,
прочитана опубликованная версия1. **Review, Apply, Undo и Publish в этом
запуске не выполнялись**, модель и каталог не вызывались; budget175/300
сохранён. Свежий результат запуска отделён от репетиции8 октября в
`.storage/presentation/reviewer-startup-status-20261009.json`.

После переноса двух forward на отдельный Reviewer master, 9 октября
10:01:38 МСК, повторены только узкие GET: anonymous login401,
authenticated login200, authenticated Synchrony200 и точная TLS-проверка.
Браузерный editor-check остался датирован09:55:29 МСК; Review по-прежнему
не повторялся. Эти timestamps разделены в свежем startup metadata.

Сертификат self-signed, **не от доверенного CA**: браузер предупредит.
Перед локальным исключением сверить fingerprint из `status.json` с
сертификатом браузера. Не принимать неизвестное предупреждение автоматически
и не отключать проверку TLS глобально. Для scripted checks использовать
конкретный `tls.crt` как trust anchor, не `curl -k`.

Текущий сертификат действует до **10 октября 2026, 17:43:42 UTC**
(20:43:42 МСК), SHA-256 fingerprint:

```text
21:98:B2:03:98:E3:C8:DF:F7:0F:72:F3:EB:F2:AB:F0:89:67:36:01:86:CE:33:5F:B0:E8:B6:4A:6F:F4:00:32
```

HTTP-проверка gateway уже пройдена со строгой проверкой этого сертификата:
без пароля, с неверным паролем и по прямой ссылке без пароля — 401;
правильный gateway-пароль открывает login Confluence (200), а editor без
Confluence session перенаправляет на login (302).
Synchrony heartbeat без gateway-пароля —401, с ним —200 `OK`.
Повторяемый безопасный check, без model call:

```bash
node scripts/demo/check-reviewer-relay.mjs
```

Результат — `.storage/presentation/reviewer-relay-status.json`.
Успешная gateway-проверка сама по себе не означает готовность remote editor.

Полная remote-репетиция **успешно завершена 8 октября 2026**:

- Настоящий браузер прошёл gateway и Confluence login, открыл native editor
  собственной статьи5636125; загрузка редактора завершилась, кнопка
  «Проверить» доступна. TLS закреплён конкретным SPKI, произвольный
  certificate bypass не включён.
- Native Review завершился за **59.084с** в этой конкретной репетиции.
  Это измерение одного запроса, не SLA или обещание времени всех проверок.
- Первый выбранный finding исправил только
  `systmctl status demo` → `systemctl status demo`.
- «Применить» изменило только этот фрагмент; native Ctrl+Z восстановил
  полный исходный HTML в точности. Сохранились 3 таблицы и 2 макроса.
- Опубликованная версия до и после —v1: ничего не опубликовано.
  Review запрос шёл по тому же внешнему HTTPS origin.

Граница этого proof — **native Review и Apply/Undo на собственном fixture**,
не все возможности Confluence. На сохранённом скриншоте footer Confluence
показывает `Can't save changes`; сохранение и collaborative autosave
не объявляются проверенными или production-ready. Publish не нажимался.
Скриншот снят перед Apply, поэтому карточка ещё содержит исходное замечание;
это не снимок состояния после исправления.

Результат сохранён в
`apps/web/public/presentation/assets/reviewer-remote-result.png`.
При сбое во время выступления показать этот сохранённый результат и
назвать его записью репетиции, а не новым live review. HTTP-only checker
не повторяет model request и сохраняет ранее записанный remote proof.

Обязательно проверить:

1. Без gateway-пароля и с неверным паролем — 401, также для прямой ссылки статьи.
2. С правильным gateway-паролем — штатный login Confluence, не автоматический
   вход admin. Gateway `Authorization` удаляется перед upstream.
3. После Confluence login открывается собственная QAEVALUI статья
   `/confluence/pages/editpage.action?pageId=5636125`.
4. Editor, кнопка «Проверить», завершение native review, Apply/Undo и
   неизменённая опубликованная версия проверены отдельно.

HTTP200 логина не доказывает remote editor: editor требует отдельной
proxy-проверки. Подготовительный helper не меняет Tomcat, Base URL или
frozen plugin; необходимую настройку **только собственного clone**
оператор выполнил отдельно, как описано далее.

## Временная HTTPS-настройка только собственного clone

Первый remote login завершался 403 на
`/confluence/rest/tsv/1.0/authenticate`. Own clone log подтвердил
`XsrfResourceFilter`: приложение видело `http://192.0.2.122:15174`,
а браузер отправлял Origin `https://192.0.2.122:15174`.
Неверный пароль не был причиной этой ошибки. Дополнительная gateway-auth
и CSRF оставлены включёнными.

В **одном** connector собственного файла
`/path/to/Projects/confluence-ai-reviewer/diagnostics/quality/20261007/stand/conf/server.xml`
изменены только `scheme="https"`, `secure="true"`,
`proxyName="192.0.2.122"`, `proxyPort="15174"`.
`SSLEnabled="false"`, port23990 и loopback address сохранены: TLS по-прежнему
завершается на nginx, не на Tomcat. Native admin Base URL временно установлен
в `https://192.0.2.122:15174/confluence`. Это стандартная
[настройка HTTPS reverse proxy для Confluence 9.2](https://confluence.atlassian.com/conf92/running-confluence-behind-nginx-with-ssl-1477577262.html);
назначение connector attributes описано в
[официальной справке Tomcat](https://tomcat.apache.org/tomcat-9.0-doc/config/http.html).

Исходный XML сохранён в
`.storage/reviewer-internal-access/server.xml.pre-https-relay` (0600);
`.storage/reviewer-internal-access/clone-proxy-change.json` (0600)
фиксирует оба Base URL и точный scope. Исходный SDK, общая модель,
plugin JAR, настройки генерации и корпоративные статьи не менялись.
Перезапущен только `confluence-qaeval-20261007.service`.

Независимая read-only проверка после restart, 8 октября 21:04:56 МСК:
frozen SHA-256 `ca4a77e1cf38df3a8cb35eddb9f8c08fe7a5c3ed64dffa88cf4c27d5496e6531`
совпадает с persisted Felix origin bundle294; 104 runtime entries
(classes/prompts) и 15 native resource entries совпадают, отличий нет.
Recorder budget остался173/300, active0; эта проверка не вызвала модель
или каталог. Это proof неизменности сборки, не proof качества review.

## Откат настройки clone после показа

Не выполнять откат до окончания live demo. Сначала вернуть Base URL,
пока HTTPS gateway и текущая Confluence session ещё доступны:

1. В HTTPS Confluence: General Configuration → Edit → Server Base URL,
   вернуть **точное исходное** значение
   `http://zakhar-Lenovo-V15-G4-AMN:1990/confluence` из
   `clone-proxy-change.json`. Сохранить только эту настройку.
2. Сверить текущий connector с private XML backup. Если после настройки
   появились другие изменения, не перезаписывать их всем backup; сверить
   с владельцем. Для текущего точного состояния передать следующий
   узкий patch через `apply_patch`:

```diff
*** Begin Patch
*** Update File: /path/to/Projects/confluence-ai-reviewer/diagnostics/quality/20261007/stand/conf/server.xml
@@
-    <Connector SSLEnabled="false" URIEncoding="UTF-8" connectionTimeout="20000" emptySessionPath="cargo.tomcat.connector.emptySessionPath" maxParameterCount="1000" maxThreads="48" port="23990" address="127.0.0.1" protocol="HTTP/1.1" redirectPort="8443" scheme="https" secure="true" proxyName="192.0.2.122" proxyPort="15174"/>
+    <Connector SSLEnabled="false" URIEncoding="UTF-8" connectionTimeout="20000" emptySessionPath="cargo.tomcat.connector.emptySessionPath" maxParameterCount="1000" maxThreads="48" port="23990" address="127.0.0.1" protocol="HTTP/1.1" redirectPort="8443" scheme="http" secure="false"/>
*** End Patch
```

3. Перезапустить только собственный clone:

```bash
systemctl --user restart confluence-qaeval-20261007.service
curl --fail --silent --show-error --max-time 5 http://127.0.0.1:23990/confluence/status
```

Дождаться `RUNNING`, затем проверить локальный native login и собственную
QAEVALUI статью. Пара отдельных `stop`/`start` не эквивалентна `restart`:
transient definition собирается после stop и может потребовать ручного
восстановления unit. Recorder не перезапускать, не вызывать `provision`,
`install`, `rebuild` или model review для проверки отката. После этого
закрыть relay по следующему разделу.

## Завершение показа

На ноутбуке закрыть только собственный master:

```bash
REVIEWER_SOCKET="$(node -p "JSON.parse(require('node:fs').readFileSync('.storage/reviewer-internal-access/ssh-runtime.json', 'utf8')).control_socket")"
ssh -S "$REVIEWER_SOCKET" -O check intern@192.0.2.122
ssh -S "$REVIEWER_SOCKET" -O exit intern@192.0.2.122
```

После сверки ownership остановить собственный expiry timer:

```bash
systemctl --user stop reviewer-reverse-ssh-expiry-20261009-demo30h.timer
```

На сервере остановить только собственный gateway-unit:

```bash
sudo systemctl stop reviewer-internal-proxy-20261009.service
```

Откат clone описан выше. Не выполнять `pkill ssh`,
`pkill ngrok`, массовый Docker stop или удаление shared volumes.
Из-за `--rm` удаляется только ephemeral gateway-контейнер; конфигурация,
сертификат и hash в собственном серверном каталоге сохраняются. Локальный
Confluence, его статьи и recorder эта остановка не затрагивает. Удаление
сохранённых файлов согласовывать отдельно. Подготовка файлов и HTTP check
не означают, что удалённый editor проверен: его фактический результат
фиксируется отдельно.
