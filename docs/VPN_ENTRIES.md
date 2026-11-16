# Запасные входы VPN

Актуально на 14 ноября 2026 года.

> **16 ноября 2026: Marzban удалён, VLESS работает на Hiddify Manager (DE:2443).** Входы
> Marzban на DE:2096–2099 и шаги ниже, где они настраиваются, — история. Сейчас RU
> пробрасывает TCP 443, 2087, 8443, 2083 и 2085 на `10.205.0.2:2443` (DE внутри
> туннеля `wg-vpn`, с 16.11.2026), UDP 36099 — на `89.125.210.172:36099`; см. разделы
> «Проброс VLESS на RU» и «Туннель wg-vpn». AmneziaWG (UDP 39051 и 44877) не менялся.

## Как было

Опись 14 ноября (`scripts/entry_inventory.py`, вывод владельца): один вход Xray, VLESS + TCP +
REALITY на DE:2096, маскировка `www.apple.com`. RU пробрасывает на него порты 443, 2087 и 8443
(DNAT). В подписке один адрес, `45.138.157.65:443`. Порт 2096 на DE открыт только для RU.
Отдельно работает AmneziaWG напрямую в Германию.

Это один путь: если заблокируют адрес RU, VLESS у клиента пропадает целиком.

## Шаг 1: прямой вход в Германию (тот же вход, второй адрес)

В подписку добавляется второй адрес того же входа: `89.125.210.172:2096`. Ключи, пользователи и
настройки Xray не меняются, VPN не перезапускается. Клиент получает адрес при следующем
обновлении подписки и может переключиться на него, если первый не отвечает. Заблокируют RU —
работает DE; заблокируют зарубежные адреса — работает RU.

Порядок (DE). Сначала обновить бота: в нём появились вызовы таблицы адресов Hiddify Manager.

```bash
git -C /root/vpn-src pull --ff-only
bash /root/vpn-src/scripts/update_bot.sh /root/vpn-src --apply
```

Что в подписке сейчас (ничего не меняет):

```bash
docker exec -i node-rental-bot-1 python3 - < /root/vpn-src/scripts/vpn_entries.py
```

Открыть порт для всех (сейчас он открыт только для RU) и добавить адрес:

```bash
ufw allow 2096/tcp comment 'VLESS direct entry'
docker exec -i node-rental-bot-1 python3 - --add 89.125.210.172:2096 < /root/vpn-src/scripts/vpn_entries.py
```

Ожидается `VPN_ENTRIES_OK: add 89.125.210.172:2096`. Скрипт копирует маскировку и остальные
настройки первого адреса и меняет только адрес и порт. Повторный запуск ничего не меняет.

Откат:

```bash
docker exec -i node-rental-bot-1 python3 - --remove 89.125.210.172:2096 < /root/vpn-src/scripts/vpn_entries.py
ufw delete allow 2096/tcp
```

Последний адрес входа скрипт не удаляет.

Что видят клиенты: в приложении появляется второй профиль с пометкой адреса в названии. Кто
добавил одну ссылку, а не подписку, второго адреса не получит: ему нужно добавить подписку.

## Проверка с телефона

Пробник умеет мерить весь путь TLS с именем маскировки. Добавить оба адреса так (Termux):

```
bash ~/vpn-probe-src/install_probe.sh --site https://app.cyberdefency.com --entry 45.138.157.65:443:www.apple.com --entry 89.125.210.172:2096:www.apple.com
```

`ok` у обоих значит, что из этой сети доходит рукопожатие REALITY до каждого входа.

## Шаг 2: второй вид входа (gRPC + REALITY)

Рядом с нынешним входом (TCP + REALITY, DE:2096) появляется второй: VLESS через gRPC на порту
2097, со своей маскировкой. Для фильтра это другой рисунок трафика, и одно правило не закрывает
оба входа. Ключ REALITY тот же, поэтому клиентам ничего вводить не нужно: новый профиль приходит с
обновлением подписки.

Маскировку выбирают так: сайт должен отвечать по TLS 1.3 **с этого сервера**, и лучше, чтобы он не
был из самых частых (`www.apple.com` — из самых частых). Проверка ничего не меняет, её можно
запускать для нескольких имён и выбрать то, что прошло (DE):

```bash
bash /root/vpn-src/scripts/vless_transport.sh --check dl.google.com
bash /root/vpn-src/scripts/vless_transport.sh --check www.microsoft.com
bash /root/vpn-src/scripts/vless_transport.sh --check addons.mozilla.org
```

`VLESS_TRANSPORT_CHECK_OK` значит: имя отвечает TLS 1.3, порт 2097 свободен, Xray принимает новую
конфигурацию. Включение (Hiddify Manager перезапускается один раз: подключённые клиенты отключаются на
несколько секунд и подключаются сами):

```bash
bash /root/vpn-src/scripts/vless_transport.sh --apply dl.google.com
```

Что делает `--apply` по порядку: копия конфигурации Xray; новая конфигурация, проверенная
`xray -test`; перезапуск Hiddify Manager и ожидание, что слушают оба порта (2096 и 2097), иначе старая
конфигурация возвращается сразу; порт 2097 в ufw; новый вход всем пользователям
(`vpn_entries.py --attach`); настройка бота `HIDDIFY_EXTRA_INBOUND_TAGS`, чтобы его получали и
новые пользователи. Итог: `VLESS_TRANSPORT_OK`.

Откат:

```bash
bash /root/vpn-src/scripts/vless_transport.sh --rollback
```

Возвращает конфигурацию до `--apply`, закрывает порт и убирает настройку бота.

Проверка с телефона: в приложении обновить подписку, появится профиль gRPC; подключиться к нему
без Wi-Fi и без других VPN.

## Шаг 3: вход через RU под российской маскировкой

Сейчас клиент идёт на российский адрес RU с именем `www.apple.com` в рукопожатии: «Apple на
российском хостинге» — заметное несовпадение. Отдельный вход для пути через RU снимает его:
маскировка — крупный российский сайт, проброс остаётся сырым (RU ничего не расшифровывает,
REALITY заканчивается в DE), порт входа на DE открыт только для RU.

1. DE. Подобрать имя: проверка требует TLS 1.3, HTTP/2 и X25519 с этого сервера.

   ```bash
   for c in ya.ru vk.com ozon.ru; do bash /root/vpn-src/scripts/vless_transport.sh --check "$c" 2>&1 | tail -1; done
   ```

2. DE. Вход TCP + REALITY (Vision, как основной) на 2098, только для RU, со своим ключом:

   ```bash
   TRANSPORT_NETWORK=tcp TRANSPORT_TAG=VLESS-TCP-REALITY-RU ALLOW_FROM=RU_SERVER_IP \
     bash /root/vpn-src/scripts/vless_transport.sh --apply ИМЯ 2098
   ```

3. RU. Порт 2083 пробрасывался на этот вход (история: с 16.11.2026 `scripts/ru_vless_relay.sh`
   ведёт 2083, как и остальные порты, на Hiddify DE:2443, раздел «Проброс VLESS на RU»).
   Вручную те же два правила для Marzban выглядели так:

   ```bash
   iptables -t nat -A PREROUTING -p tcp --dport 2083 -j DNAT --to-destination DE_SERVER_IP:2098
   iptables -t nat -A POSTROUTING -d DE_SERVER_IP -p tcp --dport 2098 -j MASQUERADE
   netfilter-persistent save
   ```

4. DE. В подписку — адрес RU для этого входа, и без адреса DE, который панель создала сама (на
   него клиент всё равно не пробьётся: порт открыт только для RU):

   ```bash
   docker exec -i node-rental-bot-1 python3 - --tag VLESS-TCP-REALITY-RU --add RU_SERVER_IP:2083 < /root/vpn-src/scripts/vpn_entries.py
   docker exec -i node-rental-bot-1 python3 - --tag VLESS-TCP-REALITY-RU --remove '{SERVER_IP}' < /root/vpn-src/scripts/vpn_entries.py
   ```

Откат: `TRANSPORT_TAG=VLESS-TCP-REALITY-RU ALLOW_FROM=RU_SERVER_IP bash … --rollback` и удаление
двух правил iptables на RU (`-D` вместо `-A`). Целиком проброс VLESS снимает
`scripts/ru_vless_relay.sh --remove`.

## Шаг 4: вход XHTTP (по желанию)

XHTTP разбивает поток на обычные HTTP-запросы: для фильтра он похож на загрузку страниц и файлов.
Hiddify Manager v0.8.4 выдаёт его в подписке приложениям на ядре Xray (v2rayNG и подобным); в подписках
sing-box (Hiddify) и Clash такой вход пропускается: их ядра XHTTP не знают.

```bash
TRANSPORT_NETWORK=xhttp TRANSPORT_TAG=VLESS-XHTTP-REALITY \
  bash /root/vpn-src/scripts/vless_transport.sh --apply ИМЯ 2099
```

## Профиль с фрагментацией

Рядом с адресом — тот же адрес с рукопожатием TLS, разрезанным на части (`fragment_setting`
Hiddify Manager). Обычный остаётся: у одних операторов фрагментация помогает, у других сама выглядит
подозрительно.

```bash
docker exec -i node-rental-bot-1 python3 - --fragment-copy 45.138.157.65:443 < /root/vpn-src/scripts/vpn_entries.py
# убрать: --fragment-remove 45.138.157.65:443
```

## Отпечаток клиента

Всем адресам — отпечаток Chrome (uTLS): рукопожатие клиента неотличимо от браузера.

```bash
docker exec -i node-rental-bot-1 python3 - --fingerprint chrome < /root/vpn-src/scripts/vpn_entries.py
```

## Дальше

- Маскировку старого входа (`www.apple.com`) сменить тем же способом позже: это затрагивает всех
  клиентов, сначала стоит увидеть, как работают новые.
- AmneziaWG через RU: проброс UDP на RU к `DE:39051` (порт 443/udp на DE занят сайтом: HTTP/3).
- Проброс VLESS на RU после переустановки: `scripts/ru_vless_relay.sh` (раздел ниже).
  На RU также `scripts/net_tuning.sh` (BBR и fq).

Ни один вход не гарантирует доступности: что работает у какого оператора, показывают замеры.

## AmneziaWG через RU (15 ноября 2026)

С мобильной сети владельца немецкий адрес перестал отвечать: TCP-соединение открывается, но
данные не идут, а с RU тот же сервер доступен. AmneziaWG шёл только напрямую в Германию и
пропал целиком. Теперь RU пересылает UDP-порт AmneziaWG на DE так же, как TCP для VLESS.

1. RU (скрипт берётся с DE, где лежит подписанный выпуск):
   ```
   ssh de-ru 'cat /root/vpn-src/scripts/ru_awg_relay.sh' | ssh root@45.138.157.65 'bash -s -- --check'
   ssh de-ru 'cat /root/vpn-src/scripts/ru_awg_relay.sh' | ssh root@45.138.157.65 'bash -s -- --apply'
   ```
   Ожидается `RU_AWG_RELAY_OK`. Добавляются только DNAT и MASQUERADE для UDP 39051 и два
   разрешения в FORWARD; правила сохраняются (`netfilter-persistent save`). Входящий файрвол
   RU (`ru_firewall.sh`) не меняется. Убрать: тот же скрипт с `--remove`.
2. DE: новые конфиги получают адрес RU:
   ```
   bash /root/vpn-src/scripts/awg_endpoint.sh --set 45.138.157.65
   ```
   AmneziaWG перезапускается один раз; если контейнер не поднимется, прежний адрес
   возвращается сам. Посмотреть текущий адрес: тот же скрипт без аргументов.
3. Клиенты: новый конфиг в боте (`/amnezia`). Старые конфиги продолжают ходить на DE напрямую.

`de-ru` — вход на DE через RU (ProxyJump), когда прямой SSH с телефона не проходит.
`install_awg.sh` при переустановке сохраняет выбранный адрес и копирует `deploy/awg/dns`.

## Проброс VLESS на RU (обновлено 16 ноября 2026, Hiddify)

RU пересылает TCP на DE и ничего не расшифровывает: REALITY заканчивается в DE, в
Hiddify Manager. Так правила стоят на сервере сейчас. После переустановки RU их
возвращает одна команда, `scripts/ru_vless_relay.sh`:

- nat PREROUTING: TCP 443, 2087, 8443, 2083 и 2085 → `10.205.0.2:2443` (Hiddify на DE,
  через туннель `wg-vpn`, раздел ниже)
- nat POSTROUTING: MASQUERADE для TCP 2443 на `10.205.0.2`, выход `wg-vpn`
- mangle FORWARD: `TCPMSS --clamp-mss-to-pmtu` на SYN, чтобы большие пакеты не
  зависали в мобильных сетях
- nat PREROUTING: UDP 36099 → `89.125.210.172:36099` (Hiddify: Hysteria2 с
  обфускацией Salamander) и MASQUERADE для него. Это вход для мобильных сетей,
  где режут TCP/TLS, но пропускают UDP (16.11.2026). Порт можно сменить через
  `HY2_PORT`; порты AmneziaWG (39051, 44877) скрипт не примет.

Старые правила Marzban (→ `:2096` и `:2098` и их MASQUERADE) и прямые TCP-пробросы на
`89.125.210.172:2443` по открытому интернету `--apply` убирает сам,
уже после того, как новые на месте, так что проброс не пропадает ни на секунду.
`--check` показывает их строками `obsolete:`. Перед любым изменением скрипт сохраняет
текущие правила (`iptables-save`) в `/etc/iptables/rules.v4.before-ru-vless-relay-<время>`;
вернуть: `iptables-restore < этот_файл` и `netfilter-persistent save`.

`--apply` требует поднятый интерфейс `wg-vpn`: без него TCP-вход вёл бы в никуда,
поэтому скрипт останавливается и ничего не меняет. Адрес и интерфейс можно сменить
через `DE_TUNNEL_IP` и `TUNNEL_IF`.

Правила AmneziaWG (UDP 39051 и 44877) скрипт не трогает ни в `--apply`, ни в `--remove`.
Политика FORWARD на RU — ACCEPT, отдельных правил filter FORWARD скрипт не
добавляет. Нужен `net.ipv4.ip_forward=1`. Входящий файрвол (`ru_firewall.sh`) не
меняется. Секретов в выводе нет. Правила сохраняются (`netfilter-persistent save`).
Повторный `--apply` ничего не добавляет и копию не делает. Убрать проброс: `--remove`.

Скрипт берётся с DE, где лежит подписанный выпуск:

```
ssh de-ru 'cat /root/vpn-src/scripts/ru_vless_relay.sh' | ssh root@45.138.157.65 'bash -s -- --check'
ssh de-ru 'cat /root/vpn-src/scripts/ru_vless_relay.sh' | ssh root@45.138.157.65 'bash -s -- --apply'
```

Ожидается `RU_VLESS_RELAY_OK`.

## Туннель wg-vpn RU↔DE (16 ноября 2026)

На открытом участке RU → DE много подключений VLESS Reality обрывается: замер с RU
напрямую на DE:2443 — 20 из 48 удачных. Те же 6 вариантов через туннель — 48 из 48.
Поэтому TCP-вход RU ходит на DE внутри отдельного WireGuard-туннеля, не через `wg-db`:

| | RU | DE |
|---|---|---|
| Интерфейс | `wg-vpn`, `10.205.0.1/30` | `wg-vpn`, `10.205.0.2/30` |
| Порт | UDP 51821 (слушает) | UDP 51822, соединяется с RU:51821, keepalive 25 с |
| Файлы | `/etc/wireguard/wg-vpn.conf`, `wg-vpn.key`, `wg-vpn.pub` | то же |
| Служба | `wg-quick@wg-vpn` (enabled) | то же |

MTU 1420; MSS на пересылаемых SYN уже ограничивает правило `TCPMSS --clamp-mss-to-pmtu`.
Ключ в конфиге не лежит: `PostUp = wg set %i private-key /etc/wireguard/wg-vpn.key`.
На RU входящий UDP 51821 открыт только с DE (`ru_firewall.sh`, `VPN_TUNNEL_PEER`,
`VPN_TUNNEL_PORT`). На DE политика INPUT — ACCEPT, отдельного правила не нужно.
Hysteria2 (UDP 36099) и AmneziaWG идут мимо туннеля, как раньше.

Поднять заново после переустановки (на каждом сервере свой ключ, ключи не печатать):

```
umask 077; cd /etc/wireguard; wg genkey > wg-vpn.key; wg pubkey < wg-vpn.key > wg-vpn.pub
# wg-vpn.conf: Address, ListenPort, MTU 1420, PostUp как выше; [Peer] PublicKey другой
# стороны, AllowedIPs = адрес другой стороны /32; на DE ещё Endpoint = 45.138.157.65:51821
# и PersistentKeepalive = 25
systemctl enable --now wg-quick@wg-vpn
```

Потом на RU — `ru_vless_relay.sh --apply`. Проверка: `wg show wg-vpn latest-handshakes`
и `ping 10.205.0.1` с DE.

Откат на прямой проброс: `iptables-restore < /etc/iptables/rules.v4.pre-tcp-via-wgvpn-*`
и `netfilter-persistent save` на RU (или выпуск скрипта до этого изменения), затем
`systemctl disable --now wg-quick@wg-vpn` на обоих серверах.

На RU, как и на DE, включаются BBR и fq — `scripts/net_tuning.sh`:

```
ssh de-ru 'cat /root/vpn-src/scripts/net_tuning.sh' | ssh root@45.138.157.65 'bash -s -- --apply'
```

`bash scripts/net_tuning.sh --check` только печатает текущие значения.
