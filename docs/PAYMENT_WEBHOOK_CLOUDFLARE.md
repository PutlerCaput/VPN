# Cloudflare 1010 на платёжном webhook

> **Устарело 7 ноября 2026.** Уведомления ЮKassa больше не используются, а
> Cloudflare Tunnel на RU выводится из работы: бот сам опрашивает API ЮKassa
> каждые 15 секунд по свежим счетам. Порядок действий — в
> [FINISH_RUNBOOK.md](FINISH_RUNBOOK.md). Текст ниже оставлен для истории.

Последняя внешняя проверка GET `/` и POST `{}` на
`https://pay.cyberdefency.com/yookassa/webhook` вернула HTTP 403 и Cloudflare
error 1010. Это блокировка по сигнатуре клиента (Browser Integrity Check),
а не ответ обработчика бота. Это наблюдение относится к проверочному запросу;
получение настоящих уведомлений ЮKassa им не проверено.

## Исправление в панели домена

1. Откройте Cloudflare → `cyberdefency.com` → Rules → Configuration Rules.
2. Создайте правило `Payment webhook POST` с Custom filter expression:

   ```text
   (http.host eq "pay.cyberdefency.com" and http.request.uri.path eq "/yookassa/webhook" and http.request.method eq "POST")
   ```

3. Добавьте настройку **Browser Integrity Check → Off** и сохраните правило.
   Проверьте порядок правил: последующее совпадающее правило не должно включать
   эту настройку обратно. Для сайта целиком защиту отключать не требуется.
4. Убедитесь, что другие правила не требуют от этого конкретного webhook
   CAPTCHA, JS challenge или входа в Cloudflare Access. Машинные уведомления
   не проходят браузерную авторизацию. Не отключайте все WAF-правила целиком.
5. В Zero Trust → Networks → Connectors/Tunnels проверьте Healthy и маршрут
   `pay.cyberdefency.com` → `http://127.0.0.1:18080` на RU.
6. Выполните с внешней машины:

   ```bash
   curl --silent --show-error --max-time 15 --output /dev/null \
     --write-out 'WEBHOOK_HTTP=%{http_code}\n' \
     -X POST -H 'Content-Type: application/json' --data '{}' \
     https://pay.cyberdefency.com/yookassa/webhook
   ```

При включённом провайдере ЮKassa обработчик новой версии возвращает 200 на
пустое уведомление. Это проверка доступности, не платёж и не активация подписки.
404 требует проверки настройки провайдера/маршрута, 502/504 — upstream,
403 с 1010 — правило Cloudflare всё ещё блокирует запрос.

Подтверждение платежа внутри бота остаётся обязательным: он получает сведения
из авторизованного API магазина и сравнивает ID, метаданные, валюту и сумму.
Одно публичное POST-уведомление не может само по себе начислить подписку.

Токен, привязанный к облачной среде агента, при последней проверке получил 401.
Изменить правило через API до восстановления учётных данных невозможно.
Замените секрет `CLOUDFLARE_API_TOKEN` через настройки среды, не через чат.
