# Состояние развёртывания по выводу владельца — 2 ноября 2026

Этот документ фиксирует присланные результаты. Прямого SSH-доступа агента к
серверам нет; значения являются наблюдением на момент проверки.

## 13 ноября 2026 — оплата СБП по QR и этап 2 (вывод владельца)

**Причина, почему страница оплаты не загружалась (вывод владельца 12 ноября).** Ссылка на
оплату отвечает `HTTP 200`, заказ жив («Не оплачен»), журнал бота чистый. Страница ЮMoney
загружает своё приложение с CDN `static.yoomoney.ru`; DNS телефона даёт узел 80.92.207.16
(Иркутск), он не принимает соединения (`Connection timed out`), отсюда белый экран. Узел в
Москве 194.186.91.126 отвечал, с DE CDN открывался (23.82.138.118). Бот и серверы не
причина.

**Исправление (PR #25): подтверждение СБП по `qr`.** ЮKassa отдаёт ссылку СБП
`https://qr.nspk.ru/...`, бот сам рисует QR-код и даёт кнопку приложения банка; страница
ЮMoney не нужна. Порядок, проверка и откат — [PAYMENTS_SBP_QR.md](PAYMENTS_SBP_QR.md).

**Выполнено на серверах (Termux, DE).**
- Бот обновлён: `UPDATE_OK node-rental-bot:latest -> node-rental-bot:update-20261113T032024Z-336428`.
- Проверка магазина перед переключением (тестовый платёж 10 ₽, оплатить его нельзя):
  `test_payment_status: pending`, `confirmation_type: qr`, `link_host: qr.nspk.ru`,
  `test_shop: false` (боевой магазин), `PAYMENT_QR_CHECK_OK`.
- `update_bot.sh --set YOOKASSA_CONFIRMATION=qr`: `SET_OK YOOKASSA_CONFIRMATION`.
  `--payment-status`: `"sbp_confirmation": "qr"`. `--doctor`: `DOCTOR: 26 ok, 0 warnings,
  0 problems` (на одну строку больше прежних 25: режим СБП).
- Сайт обновлён: `WEB_UPDATE_OK node-rental-web:release-20261112T144404Z ->
  node-rental-web:release-20261113T032853Z`; проверки `/`, `/tma/`, `/oferta` — 200,
  `/metrics` и `/internal/abuse` — 404, `/api/v1/me` без входа — 401,
  `WEB_AND_DATABASE_CHECK_OK`. Страница оплаты сайта показывает QR-код (проверено в
  браузере с подставленными ответами сервера: QR считывается в ту же ссылку СБП).
- Владелец оплатил рубли через бота по новой схеме (его слово: «Оплатил»). Это и есть
  подтверждение «платежи идут» для этапа 2.

**Этап 2 вывода старой платёжной цепочки: завершён на обоих серверах.**
- DE: отчёт `retire_payment_edge.sh` (без параметров, только чтение) после `--apply`:
  `nothing is left`, `RETIRE_REPORT_DONE`. `--doctor`: `DOCTOR: 26 ok, 0 warnings,
  0 problems` (в нём «YooKassa API reachable» напрямую, то есть оплата от удалённого не
  зависела). Вывод самого `--apply` владелец не присылал: результат подтверждён отчётом.
- RU: `retire_payment_edge.sh --apply` выполнен. Проверка целостности (`--check`, 05:47 UTC)
  показала 47 записей: 29 удалённых и 18 изменённых, добавленных 0; `dpkg.txt` и
  `rkhunter.txt` пусты. Все удалённые — файлы пакета `squid` (`/usr/sbin/squid`,
  `squid.service`, `/etc/squid/*`, `/etc/init.d/squid`, 7 ссылок `K01squid`, logrotate,
  профили apparmor и ufw, хук resolvconf) и файлы цепочки (`/etc/node-payment-edge/*`,
  `wg-payments.conf`, `node-payment-proxy.service`, `pay-callback.conf.retired`). Все
  изменённые — каталоги, из которых убрали содержимое (`/etc`, `/etc/apparmor.d`,
  `/etc/init.d`, `/etc/logrotate.d`, `/etc/nginx`, `/etc/rc0.d`–`rc6.d`, `/etc/systemd/system`,
  `/etc/ufw/applications.d`, `/etc/wireguard`, `/usr/lib/systemd/system`, `/usr/sbin`).
  Постороннего нет. `--accept` запущен в фоне (`systemd-run`, см.
  [RU_HARDENING.md](RU_HARDENING.md), «Долгие запуски») и завершился в 06:40 UTC:
  `RU_INTEGRITY_ACCEPTED: today's state is the new baseline.`, затем `--status`:
  `OK integrity: checked 0 days ago, nothing changed`. Строка в журнале rkhunter
  `Please inspect this machine, because it may be infected` — его стандартная фраза при
  любых предупреждениях в прогоне; это известные предупреждения, принятые за базу
  12 ноября, новых нет.
- Урок: `--check` и `--accept` молчат много минут, а с телефона связь может оборваться;
  для них описан запуск в фоне и чтение итога.

**Перезагрузка DE (не срочно).** `--doctor` на DE пишет `a reboot is waiting (0 d)`: 13 ноября
появилась отметка `/var/run/reboot-required` (пакеты, которым нужна перезагрузка, перечислены
в `/var/run/reboot-required.pkgs`). Это строка `OK`; `WARN` будет через 14 дней. Ежемесячное
обновление сервер само не перезагружает: нужно выполнить `reboot` в тихий час, контейнеры
поднимутся сами, затем `--doctor`.

**Что дальше.**
- Перезагрузить DE в тихий час в ближайшие две недели (см. выше).
- Через несколько дней: удалить `.env.before-update-20261112T163450Z-277268` и
  `/root/docker-compose.before-secret.yml`, когда откат образа станет не нужен; остальные
  копии `.env.before-*` удалит еженедельная очистка.
- Воскресенье 15 ноября: первые запуски таймеров (контроль целостности RU около 01:30 UTC,
  очистка DE около 04:30 UTC), затем `--doctor` и `ru_integrity.sh --status`.
- Dependabot: если останутся 2 low-алерта по `deepmerge`, закрыть их в интерфейсе GitHub.
- По желанию: сообщить поддержке ЮKassa про узел CDN 80.92.207.16 (см. PAYMENTS_SBP_QR.md).

## 12 ноября 2026 — итоги дня (вывод владельца)

Из таблицы за 11 ноября владелец выполнил шаги 1–6 и 8: база на RU за шлюзом, записи
подключений включены, контроль целостности RU работает. Не выполнен шаг 7 (этап 2).

**Токен шлюза в секрете Docker (PR #20).**
- Обновление бота: `UPDATE_OK node-rental-bot:latest -> node-rental-bot:update-20261112T163450Z-277268`.
- Токен передан с RU на DE через пайп с телефона и на экран не выводился: файл
  `secrets/db_gateway_token.txt`, 64 байта, владелец 1000:1000, права 600.
- Compose сверен (`diff`: две вставки про `db_gateway_token`), скопирован: `COMPOSE_OK`.
- `update_bot.sh --database-gateway http://10.204.0.1:8444`: `DATABASE_GATEWAY_OK`.
- Проверка: строк `DB_GATEWAY_TOKEN` в `.env` 0, в окружении контейнера 0, в `/run/secrets`
  есть `db_gateway_token`. `--doctor`: `DOCTOR: 24 ok, 1 warnings, 0 problems`
  (предупреждение про очистку, ниже).
- Ручная копия через шлюз: `BACKUP_OK /root/server-backups/backup-20261112T164339Z.tar.gz.age 236KB telegram=sent`.
- На DE лежат 14 копий `.env.before-*` и `/root/docker-compose.before-secret.yml` (без
  секретов). По размеру токен шлюза может быть только в самой свежей,
  `.env.before-update-20261112T163450Z-277268`: она больше предыдущей на 118 байт, ровно
  на строки `DATABASE_URL` и `DB_GATEWAY_TOKEN`. Её удаляют вручную (`shred -u`), когда
  откат образа станет не нужен; остальные удалит еженедельная очистка (старше 7 дней,
  кроме двух новейших).

**Контроль целостности RU (PR #21).**
- AIDE показал 5 изменений с момента базы: три записи `vpn-integrity-check.*` (два юнита
  и симлинк таймера) и два каталога под `/etc/systemd/system`. Всё это из самого
  `--apply`: база бралась раньше, чем записывались юниты. Порядок исправлен в PR #21.
- Копия скрипта на RU заменена на версию с `main` от 618dac3, хеш `4fb53ef6…7975`
  совпал на DE и RU. Замена добавила ещё два изменения (`/usr/local/sbin/ru_integrity.sh`
  и каталог), всего 7, все объяснены. PR #21 на RU не развёрнут: он влияет только на
  новую установку.
- `--accept`: `RU_INTEGRITY_ACCEPTED`, статус `OK integrity: checked 0 days ago, nothing changed`.

**Очистка DE (PR #23).**
- Еженедельная очистка пропускалась с причиной «the package manager is running» и ни
  разу не завершалась. Проверка принимала за идущее обновление постоянный процесс Ubuntu
  `unattended-upgrade-shutdown`: его имя в списке процессов обрезается до
  `unattended-upgr`. Та же проверка стояла в ежемесячном автообновлении, и оно
  пропускалось бы так же. Исправлено в PR #23.
- После исправления на DE: `OK weekly cleanup: ran 0 d ago, freed 27 MB, 4.6 GB free`.
  До очистки: `/dev/vda1 15G, 9.4G занято, 4.6G свободно, 68%`.

**Зависимости фронтендов (PR #22).** Dependabot показывал 6 алертов: `deepmerge`, `glob`,
`minimatch` в `pwa` и `tma`. Все они в цепочке `vite-plugin-pwa` → `workbox-build` из
`devDependencies` (`npm audit --omit=dev`: 0). `glob` и `minimatch` подняты точечными
`overrides` внутри `workbox-build`: сборка `pwa` побайтово та же, в `tma` тот же набор из
25 записей предкеша. У `deepmerge` исправленной версии нет ни в одном выпуске, остаются
2 low-алерта, их закрывают вручную в GitHub. Предложенное `npm audit fix --force`
(понижение `vite-plugin-pwa` до 1.1.0) проверено и ничего не чинит. Последние push GitHub
принял без уведомления об уязвимостях; список алертов у среды недоступен, смотреть в
Security → Dependabot.

**Этап 2 (отчёты, ничего не удалено).**
- DE: к удалению `/etc/node-payment-edge`, `/etc/wireguard/wg-payments.conf`,
  `node-payment-backend.service`, пакет `squid`, два правила ufw (`18081/tcp on wg-payments`
  и `3128/tcp` с 45.138.157.65).
- RU: `/etc/node-payment-edge`, `wg-payments.conf`, `node-payment-proxy.service`,
  `/etc/nginx/pay-callback.conf.retired`, `/root/ru3128.txt`, пакет `squid`.
- Строк `IN USE` нет ни на одном сервере: старая цепочка не запущена, бот не настроен на
  прокси. `--apply` ждёт подтверждения владельца, что платежи идут. `--apply` на RU
  изменит файлы под `/etc`, их отслеживает AIDE: проверка целостности покажет эти
  изменения, перед `--accept` их нужно разобрать.

**Что дальше.**
- Этап 2: `--apply` на DE и RU после подтверждения платежей.
- Через несколько дней: удалить `.env.before-update-20261112T163450Z-277268` и
  `/root/docker-compose.before-secret.yml`.
- Воскресенье 15 ноября: первые запуски по таймерам (контроль целостности RU около 01:30
  UTC, очистка DE около 04:30 UTC); затем `--doctor` и `ru_integrity.sh --status`.
- Dependabot: если останутся 2 low-алерта по `deepmerge`, закрыть их в интерфейсе GitHub.

## 11 ноября 2026 — что готово в репозитории и что осталось сделать на серверах

Код слит в `main` (PR #14–#17). На серверах с 11 ноября ничего из этого не выполнялось:
доступа к ним у среды нет, команды запускает владелец в Termux.

| № | Что | Команда (Termux) | Чем заканчивается |
|---|---|---|---|
| 1 | Обновить код и бота на DE | `RU_DATABASE_CUTOVER.md`, шаг 0 | `UPDATE_OK` |
| 2 | Обновить сайт на DE (новая политика и оферта) | `ssh de 'bash /root/vpn-src/scripts/update_web.sh /root/vpn-src --apply'` | `WEB_UPDATE_OK` |
| 3 | Подготовить перенос базы | `bash ~/db_cutover.sh prepare` | `CUTOVER_PREPARED` |
| 4 | Перенести базу (тихий час) | `bash ~/db_cutover.sh switch` | `CUTOVER_DONE` |
| 5 | Включить запись сведений о подключениях | `bash ~/db_cutover.sh connection-log on` | `CUTOVER_CONNECTION_LOG on` |
| 6 | Контроль целостности RU | `RU_HARDENING.md`, раздел про антивирус | `RU_INTEGRITY_ON` |
| 7 | Этап 2 вывода платёжной цепочки (не раньше 12 ноября, только когда `ssh de true` отвечает) | `ssh de 'bash /root/vpn-src/scripts/retire_payment_edge.sh --apply'`, затем `ssh root@45.138.157.65 'bash /root/retire_payment_edge.sh --apply'`. Файл на RU кладётся отчётным блоком из чата и не зависит от шага 3 | `RETIRE_DONE (de)`, `RETIRE_DONE (ru)` |
| 8 | Проверка | `ssh de 'bash /root/vpn-src/scripts/update_bot.sh --doctor'`, `bash ~/db_cutover.sh status` | `DOCTOR: … 0 problems` |

Порядок важен для шагов 1–5: запись сведений о подключениях (шаг 5) включается только
после того, как бот и сайт публикуют политику, где она описана (шаги 1–2). Команда шага 5
сама это проверяет по боту.

Не сделано и не будет сделано без отдельного решения: запись IP-адресов подключений;
автоматическое удаление учётных записей через 5 лет; копия архива вне RU
(`RETENTION.md`, раздел «Чего нет»).

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
- **Перезагрузка RU выполнена** 11 ноября в 00:14 UTC (тихий час): ядро 7.0.0-1017-azure,
  DNAT (3 правила) и `ip_forward=1` пережили перезагрузку, порты 443, 2087 и 8443
  отвечают, слушают только 22 и 80.
- **Пароли SSH и файрвол RU (11 ноября, после перезагрузки).** На RU `passwordauthentication
  no`. squid на DE остановлен и отключён (`inactive`, порт 3128 никто не слушает).
  `sysctl-ru.conf` и `scripts/ru_firewall.sh` доставлены через `git pull` на DE (PR #14) и
  телефон-реле. Файрвол применён и подтверждён новым входом по ключу: INPUT policy DROP,
  наружу 22 и 80, правила сохранены в `/etc/iptables/rules.v4`; порты 443, 2087 и 8443
  отвечают (rc=0), вход VPN не затронут (`RU_HARDENING.md`). Снаружи с телефона (VPN
  выключен): 22, 80, 443, 2087 и 8443 дают rc=0, а 3128, 8080, 18080 и 51820 теперь молчат
  (rc=124; до файрвола отвечали отказом, rc=1).
- **Дальше:** этап 2 вывода платёжной цепочки — 12 ноября.

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
