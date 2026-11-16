# Веб-приложение рядом с действующим Hiddify Manager

На DE уже работает `hiddify-caddy`, которому принадлежат порты 80/8443.
Обычный `docker compose up -d` из корня проекта пытается занять порт 80 ещё раз.
Для этого сервера используйте отдельный `deploy/web/compose.yaml`: веб-контейнер
слушает только `127.0.0.1:18082`, обращается к боту на `127.0.0.1:8080`
и не запускает второй экземпляр бота. Существующий Caddy завершает HTTPS.
Панель, подписки Hiddify Manager и Amnezia продолжают обслуживаться прежним стеком.

Эти файлы являются подготовкой развёртывания. Команды ниже необходимо выполнять
на DE после переноса проверенной версии проекта. Они не подтверждают, что
развёртывание уже выполнено. Перед обновлением бота нужна проверенная резервная
копия базы; веб-стек сам базу не меняет.

## 1. Проверка адресов и сборка

Войдите на DE и перейдите в корень перенесённого проекта:

```bash
cd /root/secure-node-bot/node-rental/node-rental
ss -lntp
curl --noproxy '*' --fail --max-time 5 http://127.0.0.1:8080/healthz
```

Порт 18082 должен быть свободен. Для нового HTTPS-сайта Caddy должен иметь
возможность слушать 443; если этот порт занят другим процессом, остановитесь
и проверьте его назначение. Действующие службы не останавливайте.

Введите публичное имя бота без `@` и контактный email сертификатов. Это не секреты:

```bash
read -rp 'Имя бота без @: ' BOT_USERNAME
read -rp 'Ваш email для сертификатов: ' ACME_EMAIL
export ACME_EMAIL
WEB_IMAGE="node-rental-web:release-$(date -u +%Y%m%dT%H%M%SZ)"
export WEB_IMAGE
docker build --target web --build-arg "VITE_BOT_USERNAME=$BOT_USERNAME" -t "$WEB_IMAGE" .
docker compose -f deploy/web/compose.yaml config --quiet
docker compose -f deploy/web/compose.yaml up -d --no-build
bash deploy/web/check-web.sh
```

Проверка должна завершиться `WEB_AND_DATABASE_CHECK_OK`. Она проверяет оба
интерфейса, оферту, недоступность внутренних обработчиков, требование авторизации
API и ответ базы. Успех этой проверки не доказывает, что оплаченная подписка
выдаёт рабочее VPN-подключение: это отдельная приёмка с клиентом.

Сохраните выбранный тег в эксплуатационных записях. Не заменяйте его автоматически
на `latest`; при последующих командах Compose задавайте тот же `WEB_IMAGE`.

## 2. DNS и существующий Caddy

В Cloudflare DNS создайте запись `A` с именем `app` и адресом `89.125.210.172`,
для первой установки выберите **DNS only** (серое облако). Не меняйте `panel`,
`sub`, VPN-записи и платёжный `pay`. На DE порты TCP/80 и TCP/443 должны быть доступны
для HTTPS и проверки сертификатов. Cloudflare Access/JS challenge нельзя включать
на пользовательский API и страницы авторизации без проверки Telegram Mini App.

Перед последующим включением оранжевого облака настройте доверенные актуальные
диапазоны Cloudflare на внешнем Caddy и проверьте определение реального клиентского
IP: иначе лимит запросов будет общим для посетителей, пришедших с одного адреса
Cloudflare. TLS при проксировании должен оставаться **Full (strict)**.

Создайте копию и кандидат Caddyfile, сохранив все действующие сайты:

```bash
WEB_CADDY_BACKUP="/root/Caddyfile.before-app-$(date -u +%Y%m%dT%H%M%SZ)"
cp -a /opt/hiddify-manager/Caddyfile "$WEB_CADDY_BACKUP"
cp -a /opt/hiddify-manager/Caddyfile /root/Caddyfile.app-candidate
cat deploy/web/frontdoor.caddy >> /root/Caddyfile.app-candidate
docker exec -i hiddify-caddy caddy validate --config /dev/stdin --adapter caddyfile < /root/Caddyfile.app-candidate
```

Добавляйте сайт только один раз: если `app.cyberdefency.com` уже существует
в конфигурации, обновите соответствующий блок, а не дублируйте его. Продолжайте
только после успешной валидации. Запись поверх существующего файла сохраняет inode
bind mount; замена файла через `mv` может оставить контейнер на старой конфигурации.

```bash
cat /root/Caddyfile.app-candidate > /opt/hiddify-manager/Caddyfile
if ! docker exec hiddify-caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile; then
  cat "$WEB_CADDY_BACKUP" > /opt/hiddify-manager/Caddyfile
  docker exec hiddify-caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
fi
bash deploy/web/check-web.sh https://app.cyberdefency.com
```

Если публичная проверка не проходит, сохранённая копия позволяет вернуть прежние
сайты. Не переустанавливайте Caddy и не удаляйте его Docker-тома сертификатов.

## 3. Ссылки бота и Telegram

После успешной публичной проверки задайте в конфигурации бота:

```dotenv
PUBLIC_BASE_URL=https://app.cyberdefency.com
TMA_URL=https://app.cyberdefency.com/tma/
OFFER_URL=https://app.cyberdefency.com/oferta
```

Это настройки следующего проверенного обновления **только бота**; их изменение
на диске не перезагружает контейнер автоматически. В BotFather настройте домен
Telegram Login через `/setdomain`: `app.cyberdefency.com`. Кнопке Mini App задайте
`https://app.cyberdefency.com/tma/`. Не запускайте второй polling-экземпляр бота.

Проверьте оферту без входа, приложение внутри Telegram на Android, вход в PWA,
кнопки оплаты и связи с поддержкой. Публичная платёжная цепочка через
`pay.cyberdefency.com` и RU устанавливается отдельно; успешный сайт не заменяет
проверку `payment.succeeded` и фактической активации подписки.

## Откат веб-сайта

В том же SSH-сеансе, где задан `WEB_CADDY_BACKUP`:

```bash
test -s "$WEB_CADDY_BACKUP"
cat "$WEB_CADDY_BACKUP" > /opt/hiddify-manager/Caddyfile
docker exec hiddify-caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose -f deploy/web/compose.yaml stop app-web
```

После нового входа сначала укажите точный путь сохранённой копии и `WEB_IMAGE`
из шага 1. Для отката одного обновления веб-контейнера можно сохранить сайт
и запустить Compose с прежним проверенным тегом. База бота при откате веб-сайта
не восстанавливается из старой копии.
