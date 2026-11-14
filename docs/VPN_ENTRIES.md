# Запасные входы VPN

Актуально на 14 ноября 2026 года.

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

Порядок (DE). Сначала обновить бота: в нём появились вызовы таблицы адресов Marzban.

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
конфигурацию. Включение (Marzban перезапускается один раз: подключённые клиенты отключаются на
несколько секунд и подключаются сами):

```bash
bash /root/vpn-src/scripts/vless_transport.sh --apply dl.google.com
```

Что делает `--apply` по порядку: копия конфигурации Xray; новая конфигурация, проверенная
`xray -test`; перезапуск Marzban и ожидание, что слушают оба порта (2096 и 2097), иначе старая
конфигурация возвращается сразу; порт 2097 в ufw; новый вход всем пользователям
(`vpn_entries.py --attach`); настройка бота `MARZBAN_EXTRA_INBOUND_TAGS`, чтобы его получали и
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

3. RU. Новый порт пробрасывается на этот вход (2083 — пример свободного порта):

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
двух правил iptables на RU (`-D` вместо `-A`).

## Шаг 4: вход XHTTP (по желанию)

XHTTP разбивает поток на обычные HTTP-запросы: для фильтра он похож на загрузку страниц и файлов.
Marzban v0.8.4 выдаёт его в подписке приложениям на ядре Xray (v2rayNG и подобным); в подписках
sing-box (Hiddify) и Clash такой вход пропускается: их ядра XHTTP не знают.

```bash
TRANSPORT_NETWORK=xhttp TRANSPORT_TAG=VLESS-XHTTP-REALITY \
  bash /root/vpn-src/scripts/vless_transport.sh --apply ИМЯ 2099
```

## Профиль с фрагментацией

Рядом с адресом — тот же адрес с рукопожатием TLS, разрезанным на части (`fragment_setting`
Marzban). Обычный остаётся: у одних операторов фрагментация помогает, у других сама выглядит
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

Ни один вход не гарантирует доступности: что работает у какого оператора, показывают замеры.
