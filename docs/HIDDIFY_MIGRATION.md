# Переезд с Marzban на Hiddify Manager: аудит и план

Аудит read-only, 16.11.2026. На серверах ничего не менялось, кроме копии базы Marzban
в `/root/backups/` на основном сервере. **Каждый шаг, который меняет сервер, выполняется
только после явного «да» владельца.** Секреты (ключи Reality, UUID, пароли, API-ключ)
в этот документ не входят.

## 1. Факты аудита

| Что | Значение |
|---|---|
| Основной сервер `89.125.210.172` | Ubuntu 24.04.5, ядро 6.8, **1 vCPU** Xeon Skylake, **2 ГБ RAM** (+512 МБ swap, занято ~445 МБ), диск 15 ГБ, **свободно 2,9 ГБ (80%)** |
| Где он на самом деле | HOSTKEY (AS57043). Задержка 1,2 мс до Hetzner Helsinki и 24–35 мс до Нюрнберга/Фалькенштайна, три геобазы: **Хельсинки, Финляндия**, а не Германия |
| Часовой пояс и hostname | `+0330`, hostname `cheburnetnash.com` (поменял установщик Hiddify) |
| Marzban | `gozargah/marzban:v0.8.4`, Xray 24.12.31, `network_mode: host`, `/opt/marzban`, БД `/var/lib/marzban/db.sqlite3` |
| Входы Marzban (Reality) | 2096 TCP (SNI www.apple.com), 2097 gRPC (dl.google.com), 2098 TCP-RU (vk.com), 2099 XHTTP (www.apple.com) |
| Пользователи Marzban | 3: 2 активных, 1 отключённый; у каждого 1 VLESS id, flow `xtls-rprx-vision`; суммарно ~0,3 ГБ |
| Hiddify Manager | **Уже установлен** нативно (systemd), версия 14.2.0, `/opt/hiddify-manager`. Запущен 15.11 около 19:35 по Хельсинки, SSH-сессия шла через туннель AmneziaWG (кто ставил — неизвестно). 1 пользователь (по умолчанию), домены: только IP и `89.125.210.172.sslip.io`, Reality SNI `www.wikipedia.org`. Включены почти все протоколы (vmess, trojan, hysteria, tuic, ssh, naive, mieru, anytls, snell, socks, WireGuard, Telegram-прокси). WARP выключен |
| Порты Hiddify | `rpxy-l4` на **0.0.0.0:443** (TCP/UDP), haproxy на :80, панель 127.0.0.1:9000, MariaDB, Redis, xray и hiddify-core на внутренних портах |
| **Конфликт 443** | `marzban-caddy` тоже слушает `*:443`. По IPv4 порт 443 теперь получает Hiddify: `https://app.cyberdefency.com` отдаёт чужой сертификат и страницу-заглушку Hiddify. **Mini App, сайт и всё, что идёт через `PUBLIC_BASE_URL`, по прямому адресу сломаны с момента установки Hiddify.** `sub.cyberdefency.com:8443` (Caddy → Marzban) работает |
| Бот | `node-rental-bot-1`, образ `node-rental-bot:latest` от 14.11, собран из `/root/secure-node-bot/node-rental/node-rental` (копия без git, старый код на Marzban). Секреты в старом compose: `marzban_password` и другие. Режим polling. База — через шлюз `http://10.204.0.1:8444` на RU-сервере (WireGuard `wg-db`) |
| AmneziaWG | `cyberdefency-awg` (UDP 39051, DNS-фильтр unbound без журнала запросов), `amnezia-awg2` (UDP 44877) — **не трогаем** |
| Сайт | `node-rental-web-local-app-web-1` из `/root/vpn-src/deploy/web`, за Caddy (`127.0.0.1:18082`) |
| RU-реле `45.138.157.65` | 868 МБ RAM. DNAT TCP 443/2087/8443 → :2096, 2083 → :2098, UDP 39051/44877; шлюз базы бота на 10.204.0.1:8444 |
| Журналы посещений | Access-журнал Xray в Marzban и Hiddify в логах не найден (0 строк `accepted`), DNS-фильтр AWG запросы не пишет. Для Hiddify проверить ещё раз после настройки |
| DNS-фильтр для VLESS | **Нет.** Сервер резолвит через 8.8.8.8/1.1.1.1; фильтр работает только для AmneziaWG. Тексты и оферта исправлены соответственно |
| Подписка Hiddify | `https://<домен>/<user_proxy_path>/<uuid>/` сама определяет клиента по User-Agent: v2rayNG и Hiddify получают конфиг, браузер — страницу. Проверено на сервере для v2rayNG и Hiddify; NekoBox, Karing, OneXray, v2rayN не проверялись |

## 2. Шаг 0 — срочно: вернуть сайт и Mini App (нужно «да»)

`systemctl stop hiddify-rpxy-l4` освобождает IPv4:443 для Caddy. У Hiddify сейчас нет
клиентов, поэтому никто не пострадает. Откат — `systemctl start hiddify-rpxy-l4`.
Проверка: `curl -sI https://app.cyberdefency.com` с RU-сервера отвечает с правильным
сертификатом. Чтобы служба не поднялась после перезагрузки до шага 1, временно
`systemctl disable hiddify-rpxy-l4`.

## 3. План переезда

Простой для AmneziaWG — **ноль**, его не трогаем. Простой VLESS на стороне сервера — до
5 минут на шаге 5. Но клиентам в любом случае нужно заново импортировать ссылку: адрес
подписки меняется, ключи Reality и SNI у Hiddify другие. С 2 активными пользователями это
пара сообщений.

1. **Настроить Hiddify рядом, не трогая клиентов** (нужно «да»).
   - Бэкап: Backup в панели Hiddify и `mysqldump hiddifypanel` в `/root/backups`.
   - Порты Hiddify перевести с 443/80 на свободные, например `tls_ports=2443`, `http_ports=2080`. Тогда 443 остаётся Caddy, и конфликт уходит насовсем.
   - Выключить протоколы, которых нет в оферте: vmess, trojan, hysteria, tuic, ssh, naive, mieru, anytls, snell, socks, http-proxy, WireGuard Hiddify, Telegram-прокси. Оставить VLESS + Reality (TCP, gRPC) и XHTTP.
   - Reality: SNI как в Marzban (`www.apple.com`; для входа через RU — `vk.com`), свои ключи.
   - Явно отключить access-журнал (`log.access = none`) и проверить, что его нет.
   - Создать админа для API, задать `proxy_path_admin`/`proxy_path_client`, домен для подписок (например `sub2.cyberdefency.com` → 89.125.210.172) с сертификатом.
   - На RU-реле добавить **новый** DNAT, например TCP 2085 → :2443 (старые правила не трогаем).
   - Откат: восстановить бэкап Hiddify или `systemctl stop hiddify-*`; Marzban работает всё это время.
2. **Тест** (нужен владелец с телефоном): тестовый пользователь Hiddify, подключение напрямую и через RU-реле, 2ip.ru показывает Финляндию, скорость, нет утечки DNS.
3. **Перенос пользователей с теми же UUID** (нужно «да»):
   - `python3 scripts/migrate_marzban_users.py export --marzban-db /var/lib/marzban/db.sqlite3 --out /root/backups/mz_users.json` (только чтение, файл 0600).
   - В контейнере нового бота: `... import --map /tmp/mz_users.json` (план), затем `--apply`. Скрипт создаёт в Hiddify пользователей с прежним VLESS id и сроком из базы бота и записывает UUID в `node_username`. Пользователей без UUID бот теперь сам переводит на новый UUID при оплате.
   - Откат: удалить созданных пользователей в Hiddify; `node_username` вернуть из бэкапа базы (`update_bot.sh` делает его перед выкаткой).
4. **Выкатить бота на Hiddify** (нужно «да»): собрать из `main` в `/root/vpn-update`, перенести `.env` на новые переменные (`HIDDIFY_URL`, `HIDDIFY_ADMIN_PROXY_PATH`, `HIDDIFY_USER_PROXY_PATH`, `SUBSCRIPTION_BASE_URL`, `MAIN_LOCATION_NAME=Finland`), секрет `hiddify_api_key` владелец вводит сам (`update_bot.sh --set-secret`), затем `update_bot.sh /root/vpn-update --apply`. Скрипт сам откатывается при нездоровом контейнере. Ручной откат: `update_bot.sh --rollback node-rental-bot:rollback-<stamp> <.env backup>`. Порядок шагов 3 и 4 — один заход: бот на Hiddify и пользователи в Hiddify появляются одновременно.
5. **Переключить вход** (нужно «да»; это и есть простой до 5 минут): на RU-реле перевести DNAT 443/2087/8443/2083 на порт Hiddify, разослать двум пользователям новую ссылку (или они нажимают «🔄 Сбросить ссылку»). Перед сменой сохранить `cp /etc/iptables/rules.v4 /etc/iptables/rules.v4.pre-hiddify`; откат — `iptables-restore < /etc/iptables/rules.v4.pre-hiddify`.
6. **Неделя наблюдения**: Marzban остаётся запущенным, но без трафика. Смотреть RAM/swap, ошибки бота, жалобы.
7. **Удаление Marzban — только после отдельного «да» владельца.** `docker compose -f /opt/marzban/docker-compose.yml stop marzban`, затем `rm` только сервиса `marzban`; архив `/var/lib/marzban` в `/root/backups`; образ удалить. **Caddy из того же compose не удалять**: он обслуживает `app.cyberdefency.com` — сначала вынести его в отдельный compose. Откат: `docker compose up -d marzban` из архива.

## 4. Риски

- **Память.** 2 ГБ на Hiddify (MariaDB, Redis, панель, haproxy, xray, hiddify-core ≈ 600 МБ), бот, AWG с фильтром и Marzban одновременно. Swap уже почти полный. После удаления Marzban освободится ~200 МБ. Рекомендация: 4 ГБ RAM при росте.
- **Диск.** Свободно 2,9 ГБ; бэкапы и образы занимают место — чистить старые образы `rollback-*`.
- **Неизвестный установщик Hiddify.** Проверить, кто и зачем ставил, сменить пароль/ключ админа Hiddify, проверить `authorized_keys`.
- **Клиенты, кроме Hiddify и v2rayNG**, с автоопределением подписки не проверены.
- **Один сервер без резерва**: блокировка IP по-прежнему кладёт VLESS; AmneziaWG и RU-реле — частичная страховка.
- Это технический план, а не юридическая консультация.
