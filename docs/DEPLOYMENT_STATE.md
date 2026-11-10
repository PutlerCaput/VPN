# Состояние развёртывания по выводу владельца — 2 ноября 2026

Этот документ фиксирует присланные результаты. Прямого SSH-доступа агента к
серверам нет; значения являются наблюдением на момент проверки.

## 10 ноября 2026, вечер — RU-сервер, ключи SSH, вывод платёжной цепочки (вывод владельца)

Доступа к серверам у среды по-прежнему нет: владелец присылает вывод команд с телефона
(Termux, вход по ключу) и снимки веб-консоли хостера. Подробный разбор находок — в
`PENTEST_2026-11.md`, раздел «RU-сервер».

- **RU-сервер:** RUVDS, Ubuntu 24.04 (минимальный образ), хост `ruvds-i2vxf`, 1 ГБ ОЗУ,
  диск 20 ГБ (22%). Журнал и wtmp начинаются 30 октября в 11:19 UTC: сервер создан или
  переустановлен в тот день и с тех пор работает без перезагрузки. Docker, cron, fail2ban
  и ufw не установлены.
- **Слушает:** sshd 22 (`permitrootlogin without-password`, `passwordauthentication yes`
  из `50-cloud-init.conf`, `maxauthtries 6`) и nginx 80 (страница по умолчанию). После
  этапа 1 (ниже) UDP 51820 и squid на RU больше не слушают; `127.0.0.1:18080` закрывается
  вместе с перенесённым `pay-callback.conf`.
- **Вход VPN:** DNAT TCP 443, 2087, 8443 → `89.125.210.172:2096` и MASQUERADE. До
  10 ноября в `/etc/iptables/rules.v4` был только DNAT 443; теперь сохранены все три
  (копия `rules.v4.bak-20261110`), `netfilter-persistent` включён, `ip_forward=1` в
  `/etc/sysctl.conf`. Политики iptables ACCEPT: файрвола нет.
- **Ключи сервера RU пересозданы** 10 ноября в 19:18 UTC одновременно с `/etc/shadow`
  (сброс пароля в панели RUVDS). Новый ED25519:
  `SHA256:L0WtBKN6BHyZGA4hiS6dDbhwACkASjP/RyuOM7KVUMY`, сверен через веб-консоль хостера
  (прежний начинался с `mCBDPq…`). Клиент увидел `REMOTE HOST IDENTIFICATION HAS
  CHANGED`; запись в `known_hosts` заменена после сверки, не вслепую.
- **Ключи root на RU и DE:** ПК `kuklu@GRUZZCHIKI88` (`tpSu5Fzs…`) и телефон `phone`
  (`IXUM64Yq…`). Ключ `temporary-vpn-maintenance-20261103` (`563hhaWX…`) и дубль ключа ПК
  удалены.
- **Вывод платёжной цепочки, этап 1 (10 ноября).** DE: `node-payment-backend` и
  `wg-quick@wg-payments` отключены (`disable --now`); перед этим проверено, что в окружении
  бота нет `YOOKASSA_EGRESS=ru_gateway` и `YOOKASSA_PROXY_URL`; контейнеры здоровы. RU:
  `node-payment-proxy` и `wg-quick@wg-payments` отключены, nginx `pay-callback.conf`
  перенесён в `/etc/nginx/pay-callback.conf.retired`; DNAT на месте, порты 443, 2087 и
  8443 отвечают. Откат: `systemctl enable --now …` и возврат файла. Этап 2 (удаление
  конфигов, `/root/ru3128.txt`, правил ufw DE `18081 on wg-payments`, `OUT 51820/udp` и
  `3128 от RU`, пакета squid) запланирован на 12 ноября.
- **squid на DE остановлен** 10 ноября: он служил прокси Telegram для бота на RU
  (вариант A'), а владелец бота на RU не планирует (`RU_DATABASE_PLAN.md`, раздел 10).
  `/root/ru3128.txt` на RU — вывод tcpdump от 7 ноября при отладке именно этого прокси.
- **DE:** чистка ufw 10 ноября (убрано 8 разрешений без слушателя); сервер перезагружался
  10 ноября в 12:54 UTC (причина не выяснена).
- **Дальше:** перезагрузка RU в тихий час (3:00 МСК 11 ноября) ради ядра 7.0.0-1017-azure;
  выключение паролей SSH на RU скриптом `ssh_hardening.sh` с автооткатом (выполнение и
  результат дописать); этап 2 — 12 ноября.

## 10 ноября 2026 — сетевая проверка DE-сервера (вывод владельца)

Команды из `PENTEST_2026-11.md` только читали состояние. Прямого доступа к серверу у
среды по-прежнему нет, ниже наблюдение на момент проверки.

- **Слушают наружу:** SSH 22 (пароль выключен, root только по ключу), Caddy
  80/443/8443 (443 и 8443 ещё и по UDP, HTTP/3), Xray 2096 (ufw пускает только
  RU-сервер) и squid 3128 (ufw пускает только RU-сервер; ACL squid пускает только его
  и только к `api.telegram.org:443`, остальное `deny all`, журнала нет).
- **Только внутри туннеля WireGuard `wg-payments`:** nginx `10.203.0.2:18081` (юнит
  `node-payment-backend`). Активны `wg-quick@wg-payments` и `node-payment-backend`;
  `nginx` и `node-payment-proxy` (юнит RU-стороны) выключены. Это остатки платёжной
  цепочки через RU, выведенной 7 ноября: платежи от неё не зависят
  (`NETWORK_RESILIENCE.md`). **Исправление от 10 ноября (вечер):** squid в этот список не
  входит: это прокси Telegram для бота на RU (вариант A', `TELEGRAM_PROXY.md`). Платёжные
  остатки выведены из эксплуатации, см. раздел выше.
- **Бот и панель:** бот на `127.0.0.1:8080`, Marzban на `127.0.0.1:8000`; публичных
  слушателей у них нет.
- **AmneziaWG владельца:** `amnezia-awg2` публикует `44877/udp`, Docker открывает его
  мимо ufw. README и `NETWORK_RESILIENCE.md` называют `49484`: сверьте с профилем
  клиента. Контейнера `cyberdefency-awg` (абонентский AmneziaWG) на сервере нет.
- **ufw:** входящие закрыты по умолчанию. Разрешения без слушателя: `8000/tcp`,
  `2087/tcp`, `47832/tcp`, `49484/udp` (и их IPv6-копии).
- **TLS** сайта: Let's Encrypt, действует до 5 февраля 2027.
- Ubuntu 24.04 LTS поддерживается до 2029 года. Предложение `do-release-upgrade` до 26.04
  в приветствии сервера не выполнять без плана и копии: обновление системы на рабочем
  VPN-сервере не входит в задачи.

## 6 ноября 2026 — исходники обновлены, серверы не менялись

В ветке репозитория добавлены оплата Telegram Stars, тариф «Стандарт», квоты
трафика и миграция таблицы платежей ([UPDATE_2026-11-06.md](UPDATE_2026-11-06.md)).
Доступа к VPS у среды по-прежнему нет: на серверах ничего не установлено.
Старые тестовые ключи ЮKassa не использовались.

## Уточнение владельца — 5 ноября 2026

Владелец сообщил, что подключил ЮKassa через её официальный бот, и потребовал
удалить старые тестовые ключи. `YOOKASSA_TEST_SECRET_KEY` и
`YOOKASSA_TEST_SHOP_ID` больше не использовать для запросов, новых тестов
или настройки рабочего бота. Не запрашивать их повторно. Подключение через
официальный бот само по себе не устанавливает, какой платёжный сценарий
использует наш сервер; старые тестовые привязки этого не подтверждают.

Временные изолированные БД, сценарии и состояние платёжной проверки удалены
из `/tmp`. Рабочих ключей в локальных `.env` и `secrets/` не обнаружено.
Сохранённые привязки среды ещё присутствуют: доступный API не умеет удалять
секреты и runtime variables. Их необходимо удалить в настройках среды;
не считать локальную очистку удалением из хранилища или отзывом в ЮKassa.
Состояние ключей и конфигурации на VPS не проверено и не изменялось.

## Германия — 89.125.210.172

- Ubuntu 24.04.5, x86_64, RAM 1.9 GiB; последний известный свободный диск 5.9 GiB.
- Marzban v0.8.4, Xray TCP/2096; UFW разрешает этот порт с российского relay.
- AmneziaWG `amnezia-awg2`, UDP/49484; конфигурация внутри writable layer
  контейнера, отдельного тома с ней по диагностике не обнаружено.
- Caddy/панель: TCP/8443, Marzban API: 127.0.0.1:8000.
- Бот `node-rental-bot-1`, HTTP: 127.0.0.1:8080, Compose-проект `node-rental`.
- Папка Compose: `/root/secure-node-bot/node-rental/node-rental`.

## Проверенные резервные копии

Архив `vpn-20261102T144129Z-.owner-vpn-backup.Onl1W5gv.tar.gz.gpg`
на сервере проверен расшифрованием и SHA256. Владелец подтвердил совпадение
скачанного на ПК архива по SHA256. Отдельная проверка в контейнере без сети
успешно прочитала обе восстановленные базы (бот и Marzban), проверила
integrity_check и ожидаемые таблицы. Это проверка баз, не полное восстановление
сервера. Ключ восстановления не передавался агенту. Содержимое Docker-образов
в архив не включено; прежние образы не удалять.

Перед переключением скрипт запускает создание ещё одной локальной проверенной
копии. Полный вывод с её именем в последнем сообщении владельца отсутствовал;
не утверждаем, что именно эта новая копия уже перенесена на ПК.

## Применённое исправление памяти

Образ-кандидат:
`node-rental-bot:memoryfix-20261102T145858Z`

ID:
`sha256:60f1d32c9d473d90825dd9dec1f0193ddfb0ed7cee22bc5cfb31af587ca4c273`

Прежний образ сохранён:
`node-rental-bot:before-memoryfix-20261102T145858Z`

ID:
`sha256:36d93cff698fbecbbf40d12cd62953ee9ae7268609cf1653e51fda0926a0b895`

Кандидат построен от прежнего образа, обновлён только Pydantic и его зависимости.
Версии: aiogram 3.31.0, Pydantic 2.13.5, pydantic-core 2.46.5,
pydantic-settings 2.5.2. Сначала проверен импорт, затем полный запуск с тестовой
SQLCipher-БД и без сети: /healthz успешно, /api/v1/me без токена — 401,
OOM-событий нет. После сверки исходного Compose-хеша и одиночного экземпляра
бота выполнено переключение только bot, с сохранением данных.

Владелец прислал результат:
- `TELEGRAM_AUTH_OK` — getMe успешно, без конкурентного getUpdates.
- `SOURCE_PIN_OK` — версия закреплена в серверном requirements.txt.
- `MEMORYFIX_DEPLOY_OK` — проверки переключения прошли.
- `BOT_MEMORY=174.8MiB / 512MiB` — моментальный Docker Stats после запуска.

До изменения было около 449.7 MiB в Docker Stats; снижение около 61%.
Это не доказательство отсутствия любых будущих утечек или достаточности памяти
при большой нагрузке. Нужны наблюдение и проверка ответа бота на /legal.
Публичный VPN-трафик этим изменением не проверялся и его настройки не менялись.

Ручной откат владельцем через SSH:

```sh
bash /root/vpn-memoryfix-20261102T145858Z/rollback-memoryfix.sh
```

Помощник защищён сверкой Compose/образа; изменившиеся конфигурации требуют
отдельной диагностики. База при откате образа не восстанавливается из старой
копии, чтобы не потерять новые платежи. `node-rental-bot:latest` после успешного
переключения указывает на исправленный образ. Существующие рабочие контейнеры
Marzban, AmneziaWG и Caddy не перезапускались скриптом переключения.

## Что ещё не внедрено

На production применено исправление памяти, а не весь архив нового проекта.
Минимальная телеметрия, режим проверки автобана, скрытая кнопка USDT,
read-only Telegram и новые доменные списки остаются отдельным этапом.
Перед обновлением приложения проверить исторические платежи/рефералы и
совместимость миграций с текущей БД, не раскрывая персональные данные в чате.

UFW разрешал SSH TCP/22 для всех IPv4/IPv6; ограничение администрирования
одним ПК пока не подтверждено. Android подключён через AmneziaVPN к Германии;
политика Xray автоматически на AmneziaWG не распространяется. Новая политика
фильтрации на действующих серверах не включалась. Реклама — только черновики.

## Дополнительное наблюдение владельца

После обычной работы: Docker Stats 176.6 MiB / 512 MiB, running/healthy,
RestartCount=0, OOM=false. Платёжные правки из PAYMENT_POLICY.md пока
подготовлены в облачной копии и на production не применялись.

## Выбранное направление — 3 ноября 2026

Домен Cloudflare подтверждён владельцем: `cyberdefency.com`. VPN и бот остаются
на DE. RU выбран владельцем как платёжный relay. Публичный endpoint
`pay.cyberdefency.com` подготовлен через Cloudflare Tunnel на RU loopback;
RU DNAT на публичном 443 сохраняется. Платёжный API идёт через приватный туннель
к RU CONNECT relay, а TLS и merchant key остаются на DE.

В репозитории подготовлены Cloudflare-инструкция, безопасный сценарий исправления
пустой кнопки оплаты и генератор конфигураций RU/DE. **Ничего из этого не было
установлено на VPS или применено в Cloudflare.** Домен DNS, WireGuard, Cloudflare
Tunnel, конфигурации nginx/Squid, магазин и production bot image ещё требуют
применения владельцем. Кандидат WG-подсети необходимо сверить с фактическими
маршрутами Docker и серверов перед созданием интерфейса.

## Проверка рабочей облачной копии — 3 ноября 2026

- Python 3.11: Ruff и форматирование прошли; mypy без ошибок; secret scan чистый;
  Bandit завершился успешно с предупреждением B603 на проверяемом subprocess.
- Полный набор: 165 тестов прошёл. PWA и Telegram Mini App собраны.
- Cloudflare secret binding `CLOUDFLARE_API_TOKEN` присутствует, но запрос
  `/user/tokens/verify` получил HTTP 401, код 1000 `Invalid API Token`. Значение
  секрета не выводилось. DNS и Tunnel не менялись.
- SSH-доступа к VPS из этой рабочей среды нет. Ни RU, ни DE конфигурации отсюда
  не применялись.
- В этой версии рабочая БД — SQLite/SQLCipher. Таблицы PostgreSQL
  `malicious_activity_logs` нет; отдельный архиватор нельзя запускать против
  production без подтверждённой PostgreSQL-схемы.

## Кнопка оплаты — результат владельца, 3 ноября 2026

Первая попытка `/root/fix_empty_payment_button.py` завершилась ошибкой Docker до
переключения. Владелец повторно запустил исправленный сценарий; он вывел
`PAYMENT_MENU_STAGING_OK` и `PAYMENT_MENU_FIX_DEPLOYED` для каталога
`/root/vpn-payment-menu-20261103T060540Z-3z2djzsa`. Скрипт сообщил, что платёжные
учётные данные и остальные сервисы не менялись. Это подтверждает развёртывание
обработчика кнопки, но ещё не подтверждает работу реального счёта ЮKassa.

Откат, напечатанный владельческим сценарием:

```sh
python3 /root/vpn-payment-menu-20261103T060540Z-3z2djzsa/fix.py --rollback /root/vpn-payment-menu-20261103T060540Z-3z2djzsa
```

## WireGuard and payment relay progress — 3 November 2026

- Owner-generated WireGuard keys are installed on their respective servers.
  The owner reported a recent handshake on `wg-payments` between
  `10.203.0.1` (RU) and `10.203.0.2` (DE); the payment subnet did not overlap
  the reported Docker, AmneziaWG, or host routes.
- From DE, a request through `http://10.203.0.1:3128` to
  `https://api.yookassa.ru/v3/me` returned HTTP 401 without credentials.
  This confirms the private proxy route reaches YooKassa, not merchant auth or
  a payment. The proxy ACL is intended to permit only DE to CONNECT to
  `api.yookassa.ru:443`.
- The RU host does not have UFW installed. WireGuard connectivity is confirmed;
  keep the provider firewall restricted to DE UDP/51820 rather than installing
  or enabling a host firewall remotely.
- `CLOUDFLARE_API_TOKEN` again returned HTTP 401, error 1000. DNS and Tunnel
  have not been changed. The next private callback hop is prepared as a
  dedicated DE nginx service, isolated from the existing Caddy listener.
- Local project source now greets users on `/start` and offers an off-menu
  Crypto Pay invoice with QR after a user asks about USDT and selects a tariff
  and term. The regular payment menu remains RUB/ЮKassa only. Ruff, formatting,
  mypy, and all 171 tests pass. These source changes have not been deployed to
  the production bot.

## Public offer and My Tax preflight — 3 November 2026

- The bot's offer link now points to public `/oferta`, served by Caddy without
  Telegram login. The Caddy image includes the current `legal/oferta.md`.
- The deployment preflight now rejects a live YooKassa shop unless its account
  API confirms `fiscalization_enabled=true`.
- Read-only check of the bound test shop succeeded (`test=true`,
  `status=enabled`) but reported `fiscalization_enabled=false`; no payment was
  created and no shop setting was changed. The owner must connect/enable
  self-employed fiscalization in YooKassa before automated receipts can be
  verified. Guide: `YOOKASSA_MY_TAX_SETUP.md`.
- PWA and Mini App production builds passed; the Python suite passes 174 tests.
- The web Docker image built with session CA trust; Caddy validation passed and
  a local HTTP smoke test fetched `/oferta` with HTTP 200 and the full offer.
- Neither offer nor bot changes are deployed to VPS. TCP/22 to both reported
  servers is refused from this environment; external HTTP checks for the domain
  are blocked by the managed outbound proxy. The owner-provided RU log shows
  `node-payment-backend.service` failed and RU cannot reach `10.203.0.2:18081`;
  the backend listener must be diagnosed on the DE host.

## Owner diagnostic output — 3 November 2026

- On DE, WireGuard is up with `10.203.0.2/24`. The backend fails during
  `nginx -t`: default proxy temp path `/var/lib/nginx/proxy` is read-only under
  the service's systemd hardening. The candidate now sets a writable
  `/run/node-payment-backend/proxy_temp` and creates it before Nginx starts.
- The running bot reports `TMA_URL=https://node.example.com/tma/` and
  `OFFER_URL=https://node.example.com/#/legal`; both are placeholder URLs, so
  the app and offer links cannot work for the owner's domain. `PUBLIC_BASE_URL`
  is now required and placeholder origins are rejected at startup.
- The earlier production inventory showed `marzban-caddy` already owns host
  port 80 and did not show a `node-rental-web` container. The project's web
  Compose service also binds port 80, so starting it as-is would collide with
  the live Caddy. Its `/tma` and `/oferta` routes must be merged into the live
  front door or deployed behind it before changing the bot URL.
- The payment relay config and service candidate include the temp-path fix.
  Tests and static checks pass (175 tests). None of these changes are deployed.
- The follow-up journal after that fix shows the next Nginx default temp path,
  `/var/lib/nginx/fastcgi`, is also blocked by the service's read-only policy.
  The candidate now redirects Nginx proxy/FastCGI/uWSGI/SCGI temp paths to the
  writable runtime directory. The owner stopped the failing unit; it remains
  unavailable until these latest additions are installed.
- `marzban-caddy` uses host networking and mounts `/opt/marzban/Caddyfile`.
  Its only public sites are `panel.cyberdefency.com:8443` and
  `sub.cyberdefency.com:8443`; there is still no site for the bot app domain.

## Payment confirmation behavior — 3 November 2026

- The bot treats a YooKassa notification only as a payment ID hint, then fetches
  that payment from the authenticated YooKassa API. It activates only when the
  API confirms `succeeded` and `paid=true`, the RUB amount is sufficient, and
  payment metadata matches the bot's pending order. Pending or temporarily
  unavailable API checks return 503 for retry; duplicate notifications do not
  extend the subscription twice.
- Subscription activation is persisted and retried by the bot scheduler every
  minute if Marzban is temporarily unavailable.
- Verification: all 176 tests pass, including API-confirmation, underpayment,
  retry, and duplicate-notification cases; Ruff, format check, and mypy pass.
  Read-only `GET /v3/me` against the bound YooKassa test shop returned HTTP 200,
  `test=true`, and `status=enabled`. No payment was created. The test account
  reports `fiscalization_enabled=false`, so automatic receipts are not verified.
- Production is not end-to-end ready: the owner-provided DE log shows
  `node-payment-backend.service` stopped because Nginx still tries to create
  `/var/lib/nginx/fastcgi` on a read-only filesystem. The current candidate
  redirects all Nginx temp paths to `/run`, but this latest config has not yet
  been applied on DE. The production provider was last reported as `manual`;
  test-shop credentials in this environment are not evidence that production
  credentials are installed on the bot. The public webhook also depends on the
  Cloudflare/RU relay being configured and reachable.
- A fresh read-only Cloudflare token verification from this environment returned
  HTTP 401. No DNS or Tunnel settings were changed.

## Final workspace verification — 3 November 2026

- Current checks on this draft: Ruff, formatting, mypy, secret scan, Bandit, and
  pip-audit pass; Bandit reports the existing reviewed `B603` subprocess warning.
  The complete Python suite passes 176 tests. PWA and Telegram Mini App production
  builds both pass after frozen `npm ci` installs.
- Cloudflare agent setup installed the 19 Cloudflare skills and registered
  `https://mcp.cloudflare.com/mcp` in Codex, but `codex mcp list` still reports
  `Not logged in`; OAuth requires account-owner approval. No DNS or Tunnel edits
  were made.
- The current bound Cloudflare API token fails read-only verification with HTTP
  401/code 1000. `npx wrangler ai models` using Wrangler 4.157.0 also fails at
  `/accounts` with code 9109 from this environment's egress location. The broad
  token-policy JSON supplied later did not update the environment's secret
  binding. No token value is stored in this repository.
- The latest public checks of `pay.cyberdefency.com` returned HTTP 521; the
  owner's latest RU-origin check was HTTP 000. The payment webhook is not public
  or reachable end-to-end. The last bot configuration reported
  `SBP_PROVIDER=manual`, shop ID present, and YooKassa secret absent.
- TCP egress to the VPSs is not granted in this environment. Production bot,
  Cloudflare DNS/Tunnel, YooKassa notification settings, and My Tax
  fiscalization remain external deployment steps; none were changed here.

## Subscription reminder and current draft — 3 November 2026

- The bot now checks every 15 minutes for active subscriptions entering the
  next 72 hours and sends one Telegram reminder per `expires_at` value. The
  existing `expiry_reminded_for` field prevents repeats, and a renewed
  subscription clears the marker. One-day trials are excluded so they do not
  receive a reminder immediately after activation. The reminder also uses the
  existing Web Push path when configured.
- Three focused reminder tests and the complete suite pass: 179 Python tests.
  Ruff, formatting, mypy, secret scan, Bandit, and Python dependency audit pass;
  Bandit still reports the reviewed B603 subprocess warning. This reminder has
  not been deployed to the bot on DE.
- Both PWA and Mini App now build with Vite 7.3.7 and the compatible React/PWA
  plugins. Production dependency audits (`npm audit --omit=dev`) report zero
  advisories. Full development-tree audits report 10 low/moderate findings in
  the Workbox/PWA build dependency chain (no high or critical findings). I did
  not force an incompatible Workbox downgrade; the production builds pass.
- A local Codex profile was created at `$CODEX_HOME/cloudflare-aig.config.toml`
  for the owner's account and `default` AI Gateway, using the documented
  `/openai/responses` route. Cloudflare's OpenAI provider requires an OpenAI
  API key. A secret requirement named `CLOUDFLARE_API_KEY` for
  `gateway.ai.cloudflare.com` was saved in the environment draft; it is not
  active until the owner reviews and publishes that draft. The profile has not
  sent a model request.
- Latest read-only YooKassa test-shop check returned HTTP 200 with
  `test=true`, `status=enabled`, and `fiscalization_enabled=false`. A 1 RUB test
  payment was created; the API reports it `pending` and `paid=false`. Both
  cancellation requests returned HTTP 400 `invalid_request`, so the sandbox
  invoice remains pending. No live charge occurred, but this does not validate
  successful payment capture, webhook delivery, or subscription activation.
- No Cloudflare DNS/Tunnel, YooKassa, or VPS configuration changed. The prior
  blockers remain: Cloudflare MCP is unauthenticated, the previously bound
  account token failed verification, no outbound TCP/22 is granted, and the
  payment callback was previously HTTP 521 / RU origin HTTP 000. Do not treat
  local source/tests as production deployment.
