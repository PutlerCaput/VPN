# Прокси Telegram Bot API для бота в РФ (вариант А')

> **Не используется (10 ноября 2026).** Владелец отменил вариант А': бот остаётся на DE,
> база переедет на RU отдельным этапом (`RU_DATABASE_PLAN.md`, раздел 10). Поэтому прокси
> не нужен: `squid` на DE остановлен 10 ноября, пакет и правило ufw удаляются на этапе 2
> вывода платёжной цепочки. Документ оставлен на случай, если бот когда-нибудь переедет
> на RU.

Бот в РФ не достаёт `api.telegram.org` напрямую: с RU-сервера Telegram не отвечает.
Через сервер вне РФ достаёт (проверено 7 ноября 2026: код 302 за 0.97 с).
Прокси на сервере вне РФ пускает только `api.telegram.org:443` и только с IP RU-сервера.

Адреса в примерах: DE-сервер `89.125.210.172`, RU-сервер `45.138.157.65`.
Если адрес другой, поправьте его в `squid.conf`, в правиле `ufw` и в `.env` бота.

## 1. Сервер вне РФ

```bash
apt-get install -y squid
```

Файл `/etc/squid/squid.conf` — заменить целиком:

```
# CONNECT только к api.telegram.org:443 и только с IP RU-бота
http_port 3128
acl ru_bot src 45.138.157.65/32
acl telegram_api dstdomain api.telegram.org
acl ssl_port port 443
acl connect_method method CONNECT
http_access allow connect_method ru_bot telegram_api ssl_port
http_access deny all
access_log none
cache deny all
forwarded_for delete
```

Проверить синтаксис и перезапустить:

```bash
squid -k parse && systemctl restart squid
```

Файрвол: порт 3128 открыт только для RU-сервера:

```bash
ufw allow from 45.138.157.65 to any port 3128 proto tcp
ufw status verbose    # по умолчанию входящие должны быть закрыты (deny incoming)
```

## 2. Проверка с RU-сервера

```bash
curl -x http://89.125.210.172:3128 -s -o /dev/null -w '%{http_code} %{time_total}s\n' --max-time 15 https://api.telegram.org/
```

Ожидаемо: код `302` (как в SSH-тесте). С любого другого адреса порт должен быть закрыт.

## 3. Бот на RU-сервере

В `.env` (или через скрипт обновления):

```bash
bash scripts/update_bot.sh --set TELEGRAM_PROXY_URL=http://89.125.210.172:3128
bash scripts/update_bot.sh --doctor    # строка «Telegram answers as @…» — уже через прокси
```

Откат: `bash scripts/update_bot.sh --set TELEGRAM_PROXY_URL=` и перезапуск (скрипт делает его сам при ошибке).

## Что видит прокси

- Адрес RU-бота, адрес `api.telegram.org` и объём трафика.
- Содержимое и токен бота не видны: это TLS между ботом и Telegram. Токен в пути запроса, внутри TLS.
- Журнал прокси отключён (`access_log none`), кэш выключен.

## Ограничения

- Если IP RU-сервера изменится, обновите `acl ru_bot` и правило `ufw`.
- Если канал RU → ФРГ или прокси упадут, бот в РФ перестанет получать обновления от Telegram. Нужен алерт на этот канал.
- Пароля в адресе прокси нет. Защита — только список IP и правило на домен.
