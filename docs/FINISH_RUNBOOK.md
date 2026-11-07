# Завершение запуска: порядок действий

Редакция от 7 ноября 2026. Команды выполняются по шагам; к следующему шагу
переходите, только если предыдущий дал ожидаемый результат. Секреты (токены,
пароли, ключи) в чат не отправляются.

## Что изменилось

- **Туннель Cloudflare на RU больше не нужен.** Он доставлял только
  уведомления ЮKassa и терял связь примерно каждые 3 минуты. Теперь бот сам
  опрашивает ЮKassa: свежий счёт — каждые 15 секунд, затем раз в минуту. В
  сообщении со счётом есть кнопка «🔄 Проверить оплату».
- **Один скрипт проверки всего сервера:** `update_bot.sh --doctor`. Он ничего
  не меняет и для каждой проблемы пишет команду исправления.
- **Безопасная смена адреса сайта:** `update_bot.sh --set-public-url`. Адрес
  меняется, только если сайт по нему уже открывается.

## Шаг 1. Код на DE-сервере

Если папка `/root/vpn-src` уже есть:

```bash
git -C /root/vpn-src pull --ff-only && git -C /root/vpn-src log --oneline -1
```

Если её нет, сначала один раз дайте серверу доступ к GitHub только на чтение:

```bash
ssh-keygen -t ed25519 -N "" -C "de-server-readonly" -f /root/.ssh/vpn_deploy   # если ключа ещё нет
cat /root/.ssh/vpn_deploy.pub
```

Строку `ssh-ed25519 …` добавьте на https://github.com/PutlerCaput/VPN/settings/keys
(«Add deploy key», галочку «Allow write access» не ставить). Затем:

```bash
ssh-keyscan -T 10 -p 443 -t ed25519 ssh.github.com > /root/.ssh/github_known_hosts
ssh-keygen -lf /root/.ssh/github_known_hosts   # ожидается SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU
cd /root && git clone -c core.sshCommand="ssh -i /root/.ssh/vpn_deploy -o IdentitiesOnly=yes -o UserKnownHostsFile=/root/.ssh/github_known_hosts" \
  -b ccr-67b53ac3-v9lutm ssh://git@ssh.github.com:443/PutlerCaput/VPN.git vpn-src
```

## Шаг 2. Обновить бота, затем сайт

```bash
cd /root/vpn-src
bash scripts/update_bot.sh "$PWD" --apply     # ожидается UPDATE_OK
bash scripts/update_web.sh "$PWD" --apply     # ожидается WEB_UPDATE_OK
```

При ошибке скрипты сами возвращают прежнюю версию и пишут `UPDATE_FAILED` или
`WEB_UPDATE_FAILED`.

## Шаг 3. Адрес сайта для ссылок бота

Бот строит ссылки на Mini App, оферту и политику из `PUBLIC_BASE_URL`. Сайт
работает на `app.cyberdefency.com`:

```bash
bash scripts/update_bot.sh --set-public-url https://app.cyberdefency.com   # ожидается SET_OK
```

## Шаг 4. Проверка всего сервера

```bash
bash scripts/update_bot.sh --doctor
```

Последняя строка — `DOCTOR: N ok, N warnings, N problems`. Для каждой строки
`FAIL` в ней указана команда исправления, например
`--set ACTIVITY_LOGGING_ENABLED=false` или `--set AUTOBAN_MODE=review`. Их
запускают так: `bash scripts/update_bot.sh --set AUTOBAN_MODE=review`.

## Шаг 5. Вывести туннель на RU-сервере

На **RU-сервере** (45.138.157.65):

```bash
cloudflared service uninstall
systemctl is-active cloudflared   # ожидается inactive (или unknown)
```

VPN-вход на RU (DNAT 443) и WireGuard/Squid для запросов к API ЮKassa это не
затрагивает.

## Шаг 6. Убрать остатки в Cloudflare и ЮKassa

1. Cloudflare → Zero Trust → Networks → Tunnels: удалите оба туннеля
   (старый `payments-ru` и новый).
2. Cloudflare → `cyberdefency.com` → DNS: удалите запись `pay` (CNAME на
   `…cfargotunnel.com`). Проверьте записи с лишним `.com` в имени (раньше
   встречалось `app.cyberdefency.com.com`) и удалите их.
3. Кабинет ЮKassa → Интеграция → HTTP-уведомления: удалите адрес
   `https://pay.cyberdefency.com/yookassa/webhook` или снимите все события.

## Шаг 7. Настройки в @BotFather

- Bot Settings → Menu Button → адрес `https://app.cyberdefency.com/tma/`.
- Bot Settings → Configure Mini App (если используется) → тот же адрес.
- Bot Settings → Privacy Policy → `https://app.cyberdefency.com/privacy`.
- `/setdomain` → `app.cyberdefency.com` (вход через Telegram на сайте).

## Шаг 8. Проверка в Telegram

1. Полностью закройте и заново откройте Mini App → «Тарифы» → «Оплатить».
   Приложение закроется, в чате появится меню оплаты.
2. «💳 Оплата через СБП» → кнопка «💳 Оплатить через СБП» → приложение банка.
3. После оплаты бот пришлёт «✅ Оплата получена» в течение примерно 15 секунд;
   кнопка «🔄 Проверить оплату» проверяет сразу.

Старые неоплаченные счета отменятся сами в течение часа после обновления.

## Что остаётся за владельцем

- **Где хранится рабочая база.** Бот работает с базой `data/bot.db` на DE.
  Политика обработки данных говорит, что база хранится в России (ч. 5 ст. 18
  152-ФЗ требует первичной записи в российскую базу). Если на RU лежит только
  выгруженный реестр, рабочую базу нужно перенести на RU.
- **Уведомления в Роскомнадзор** об обработке персональных данных (ст. 22) и о
  трансграничной передаче (ст. 12), на которые ссылается политика.
- **USDT.** Если `--doctor` пишет `USDT (Crypto Pay) is on`, решение за вами:
  `bash scripts/update_bot.sh --disable-usdt` выключает его.
- **Юридическая проверка** оферты и политики юристом перед публикацией.

## Если клиенты не могут создать оплату

В логе бота событие `yookassa_create_failed`; поле `error` называет причину.
Команда, которая её покажет:

```bash
docker logs --since 30m node-rental-bot-1 2>&1 | grep yookassa_create_failed | tail -1
```

- `HTTP 401` или `HTTP 403` — ID магазина и ключ не от одного магазина, либо
  ключ тестовый. Ключ лежит в файле `secrets/yookassa_secret_key.txt`, а не в
  `.env`: правка `.env` его не меняет. Порядок — раздел «Ключи ЮKassa» ниже.
- `HTTP 400 invalid_request: …` — ЮKassa не принимает запрос. Если в тексте
  есть «sbp» или «method», включите СБП: кабинет ЮKassa → Настройки → Способы
  оплаты. Текст после кода покажет точную причину.
- `transport error …` — бот не достучался до ЮKassa. Проверьте
  `YOOKASSA_EGRESS=direct` и пустой `YOOKASSA_PROXY_URL`.
- `HTTP 5xx` или таймаут — временный сбой ЮKassa. Бот повторяет сам; клиент
  видит кнопку «🔄 Повторить».

Отклонённый ЮKassa счёт отменяется сразу, и клиент может выбрать другой срок.
Счёт, по которому ЮKassa не ответила больше суток, отмечается «проверяется»:
такие случаи разбираем вручную по номеру счёта в кабинете ЮKassa.

## Ключи ЮKassa

Ключ берётся из файла `secrets/yookassa_secret_key.txt`: это Docker Secret, и он
важнее `.env`. ID магазина (`YOOKASSA_SHOP_ID`) хранится в `.env`. Оба значения
должны быть от одного магазина: кабинет ЮKassa → Интеграция → Ключи API.

Значения вводятся на сервере скрыто. В чат их не присылайте.

```bash
WORKDIR=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' node-rental-bot-1)
PROJECT=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' node-rental-bot-1)
SERVICE=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' node-rental-bot-1)

( umask 077
  read -r -p "ID магазина ЮKassa (только цифры): " SHOP
  [[ "$SHOP" =~ ^[0-9]{3,20}$ ]] || { echo "ID магазина выглядит неверно"; exit 1; }
  grep -v '^YOOKASSA_SHOP_ID=' "$WORKDIR/.env" > "$WORKDIR/.env.tmp"
  printf 'YOOKASSA_SHOP_ID=%s\n' "$SHOP" >> "$WORKDIR/.env.tmp"
  cat "$WORKDIR/.env.tmp" > "$WORKDIR/.env" && rm -f "$WORKDIR/.env.tmp"
)

( umask 077
  read -rs -p "Боевой секретный ключ ЮKassa (live_…), ввод скрыт: " KEY; echo
  [[ "$KEY" =~ ^(live|test)_[A-Za-z0-9_-]{20,200}$ ]] || { echo "Ключ не похож на ключ ЮKassa"; exit 1; }
  cp -p "$WORKDIR/secrets/yookassa_secret_key.txt" "$WORKDIR/secrets/yookassa_secret_key.txt.bak-$(date -u +%Y%m%dT%H%M%SZ)"
  printf '%s' "$KEY" > "$WORKDIR/secrets/yookassa_secret_key.txt"
  unset KEY
)

cd "$WORKDIR" && docker compose -p "$PROJECT" up -d --no-deps --no-build --force-recreate "$SERVICE"
```

Проверка: выполните блок из раздела «Если клиенты не могут создать оплату»
(`HTTP 401` → проверка `/v3/me`). Ожидаем `HTTP 200`, `shop status: enabled`,
`test shop: False`, `shop id matches: True` и `sbp` в `payment methods`. Затем
удалите резервные копии ключа: `rm -f "$WORKDIR"/secrets/yookassa_secret_key.txt.bak-*`.
Если в `.env` осталась строка `YOOKASSA_SECRET_KEY=`, её можно удалить: бот её не читает.
