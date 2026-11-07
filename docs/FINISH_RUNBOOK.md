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
