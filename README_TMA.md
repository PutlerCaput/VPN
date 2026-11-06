# Telegram Mini App

Бесплатное приложение внутри Telegram, только для просмотра: статус аренды, конфигурация с QR-кодом, реферальная ссылка, история платежей и оферта. Оплата здесь не проводится: кнопки открывают бота (`tg.openTelegramLink("https://t.me/<бот>?start=pay_<месяцы>")`), а бот показывает тарифы с уже выбранным сроком.

## Как подключено

- Фронтенд: `tma/` (React 18, TypeScript strict, Vite 5, CSS Modules), собирается с `base: "/tma/"` и отдаётся Caddy по `https://<домен>/tma/`.
- `PUBLIC_BASE_URL` обязателен и должен быть реальным публичным HTTPS-адресом веб-сервера. Если внешний TLS-порт — 8443, укажите `https://<домен>:8443`; бот строит из этого адреса кнопку приложения и ссылку на оферту. Заглушка `node.example.com` теперь отклоняется при запуске.
- Бэкенд: тот же REST API, что у PWA (`api/`, монтируется `register_api_routes(app, services)`). Отдельной базы нет: используются таблицы бота `users`, `payments`, `offer_acceptances`, `refresh_tokens`.
- Защита от повтора `initData` хранится в памяти процесса (ограниченный словарь на 10 000 записей с TTL 300 с, очистка раз в 60 с). Она намеренно не переживает перезапуск, поэтому таблица `used_initdata` из ТЗ не создаётся.
- Вместо `@tma.js/sdk` используется официальный `telegram-web-app.js` с собственной типизацией (`src/telegram.ts`): в ТЗ не была указана версия SDK, а API этой библиотеки сильно менялся между версиями. Бандл стал меньше.

## Безопасность

| Требование | Где |
|---|---|
| HMAC-SHA256 initData, `secret = HMAC(b"WebAppData", BOT_TOKEN)`, `compare_digest` | `api/auth.py::validate_init_data` |
| `auth_date` не старше 300 с | `api/auth.py::_check_auth_date` |
| защита от повтора, ограниченный кеш | `api/auth.py::ReplayGuard` |
| единый ответ `401 {"error":"unauthorized"}` | `api/routes.py::_auth_failed`, `api/middleware.py` |
| в логах только `{event, reason, ip_hash}`, без initData и токенов | `api/routes.py::_auth_failed` |
| JWT HS256: access 15 мин, refresh 30 дней в БД, ротация | `api/auth.py::TokenService` |
| лимиты: 10/мин на IP для `/auth/telegram`, 60/мин на пользователя, 30/мин на IP без токена, 10/мин на `/config` | `api/middleware.py` |
| токены в SecureStorage, иначе только в памяти | `tma/src/storage/secure.ts` |
| один refresh на параллельные 401, таймаут 15 с | `tma/src/api/client.ts`, `tma/src/auth/initData.ts` |
| CSP с `frame-ancestors` для Telegram | `Caddyfile` (блок `/tma/*`) |

## Настройка в @BotFather

1. `/mybots` → бот → Bot Settings → Menu Button → URL `https://<домен>/tma/`.
2. Либо `/newapp` для ссылки вида `https://t.me/<бот>/<app>?startapp=<telegram_id>` (параметр `startapp` используется как реферальный).

## Чек-лист

- [ ] `cd tma && cp .env.example .env`, указать `VITE_BOT_USERNAME`.
- [ ] `npm ci && npm run build` (в Docker это делает цель `web`).
- [ ] Открыть Mini App из кнопки меню: проходит вход, видна оферта, после принятия появляется пробный период.
- [ ] «Показать конфигурацию» отдаёт ссылку и QR; 11-й запрос за минуту получает «Слишком часто».
- [ ] «Оформить в боте» открывает чат с тарифами; после оплаты и возврата статус обновляется сам.
- [ ] Перезапуск Mini App на устройстве с Bot API 9.0+ не требует повторного входа; на старых клиентах вход выполняется заново по initData.
- [ ] «Выйти» отзывает сессию (повторный запрос со старым токеном → 401) и закрывает приложение.
- [ ] В Telegram Web (web.telegram.org) приложение открывается во фрейме без ошибок CSP.
- [ ] Тесты бэкенда: `pytest tests/test_initdata.py tests/test_jwt.py tests/test_replay_guard.py tests/test_rate_limit.py`.
