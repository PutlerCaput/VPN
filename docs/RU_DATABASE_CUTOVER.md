# Перенос базы на RU: порядок действий

Как устроено и почему, описано в `RU_DATABASE_PLAN.md`, раздел 11. Здесь только действия.

Все команды вводятся **в Termux на телефоне**. На серверах вручную ничего вводить не нужно:
сценарий `db_cutover.sh` сам заходит на оба сервера и переносит файлы между ними, потому что
серверы не видят ssh друг друга. База, её ключ и токен доступа на экране не показываются и на
телефоне не сохраняются.

## 0. Один раз: обновить DE и взять сценарий

```bash
ssh de 'git -C /root/vpn-src pull --ff-only && git -C /root/vpn-src log -1 --format=%h'
ssh de 'bash /root/vpn-src/scripts/update_bot.sh /root/vpn-src --apply'
ssh de 'cat /root/vpn-src/scripts/db_cutover.sh' > ~/db_cutover.sh
```

Вторая команда обновляет бота до выпуска, который умеет работать с базой на другом сервере.
Должно закончиться строкой `UPDATE_OK`. Пока адрес шлюза не задан, бот работает как раньше.

## 1. Проверка (ничего не меняет)

```bash
bash ~/db_cutover.sh check
```

Должно быть: `bot: healthy`, `database: local file`, `bot release: knows the gateway`,
в конце `CUTOVER_CHECK_DONE`.

## 2. Подготовка (бот работает, простоя нет)

```bash
bash ~/db_cutover.sh prepare
```

Что делает:

1. Копирует код шлюза с DE на RU.
2. Добавляет на RU два правила файрвола: туннель принимается только с адреса DE, порт шлюза
   открыт только внутри туннеля. Изменение подтверждается само, следующим входом сценария.
   Если связь оборвётся в этот момент, через 10 минут RU сам вернёт прежние правила.
3. Создаёт ключи туннеля на каждом сервере и поднимает туннель `wg-db`. Закрытые ключи
   остаются там, где созданы; между серверами передаются только открытые.
4. Ставит шлюз на RU (окружение Python и служба systemd), но не запускает его.

Должно закончиться строкой `CUTOVER_PREPARED`. Команду можно повторять.

## 3. Переключение (бот недоступен несколько минут)

Выбрать тихий час.

```bash
bash ~/db_cutover.sh switch
```

Что делает:

1. Останавливает бота.
2. Делает проверенную копию базы на DE.
3. Переносит на RU копию и ключ шифрования и сверяет контрольную сумму.
4. Запускает шлюз на RU. Перед запуском проверяется, что ключ открывает файл.
5. Передаёт боту адрес шлюза и токен и запускает бота.
6. Проверяет результат.

Должно закончиться строкой `CUTOVER_DONE`. После этого открыть бота и нажать /start.

Если что-то не получилось, сценарий сам возвращает бота на файл на DE и пишет причину строкой
`CUTOVER_STOPPED`. Данные при этом не теряются: до успешного шага 5 бот остановлен, и ничего
нового не записывается.

## 4. Состояние в любой момент

```bash
bash ~/db_cutover.sh status
```

## Откат

```bash
bash ~/db_cutover.sh rollback
```

Возвращает бота на файл на DE и останавливает шлюз. Откат без потерь возможен только сразу
после переключения: записи, сделанные, пока бот работал с RU, остаются на RU и в файл на DE
не попадают.

## После переноса

- **Обновления.** Сначала шлюз на RU, потом бот на DE. Шлюз применяет изменения схемы, а бот
  отказывается работать со шлюзом старее себя.

  ```bash
  ssh de 'git -C /root/vpn-src pull --ff-only'
  bash ~/db_cutover.sh update
  ssh de 'bash /root/vpn-src/scripts/update_bot.sh /root/vpn-src --apply'
  ```

- **Резервные копии.** Ночная копия на DE продолжает работать: она забирает актуальную базу с
  RU через шлюз, шифрует архив открытым ключом владельца и отправляет в Telegram, как раньше.
  Ключ шифрования базы остаётся и на DE: он входит в архив и нужен для восстановления.
- **Старый файл на DE.** `data/bot.db` на DE после переключения не обновляется. Неделю он
  нужен как запас для отката, затем удаляется (`RU_DATABASE_PLAN.md`, раздел 9).
- **Простой RU или туннеля.** Оплата, выдача, продление и личный кабинет останавливаются,
  пока связь не вернётся. Уже выданные ключи VPN при обрыве туннеля продолжают работать. Если
  недоступен сам RU, не работает и вход через него; прямое подключение к DE работает.
- **Сведения о подключениях.** Выключатель находится на RU, рядом с данными. Включается
  командой `bash ~/db_cutover.sh connection-log on`; она откажет, пока бот не публикует
  политику, в которой эти сведения описаны. Подробности в `RETENTION.md`.
- **Архив на 5 лет.** Включается шагом `prepare`, если на DE настроена резервная копия:
  архив шифруется тем же открытым ключом. Устройство и чтение архива: `RETENTION.md`.

## Если сценарий остановился

| Сообщение | Что делать |
|---|---|
| `DE does not answer` или `RU does not answer` | Проверить `ssh de true` и `ssh root@45.138.157.65 true`. Если ключ защищён паролем, сначала `ssh-add` |
| `the bot on DE is too old for the gateway` | Выполнить вторую команду из шага 0 |
| `could not confirm the firewall` | Ничего. Через 10 минут повторить `prepare` |
| `the servers do not see each other` | UDP 51820 с DE на RU не проходит: прислать вывод `bash ~/db_cutover.sh check` |
| `pip could not install the packages` | С RU недоступен pypi.org: прислать вывод |
| `the file changed on the way` | Повторить `switch` |
| `the bot did not come up on the gateway` | Бот уже работает на файле DE. Прислать вывод |

## Перенос токена в файл секрета (для уже работающего переноса)

Раньше токен шлюза лежал в `.env` на DE открытым текстом. Теперь он лежит в файле
`secrets/db_gateway_token.txt`, который Docker передаёт боту как секрет, а в `.env` его нет.

Порядок важен. Сначала токен попадает в файл, потом копируется compose, который его
объявляет, и только потом бот перезапускается с новыми настройками. Если поменять порядок,
бот при перезапуске получит пустой секрет при заданном адресе шлюза и не поднимется.
Бот перезапускается дважды, каждый раз на несколько секунд. Токен и ключи на экран не
выводятся: команды печатают только размер и количество строк.

**Termux.** Обновить код и бота на DE. Пока бот старый, токен он берёт из `.env`:

```
ssh de 'git -C /root/vpn-src pull --ff-only && git -C /root/vpn-src log -1 --format=%h'
ssh de 'bash /root/vpn-src/scripts/update_bot.sh /root/vpn-src --apply'
```

Должно закончиться строкой `UPDATE_OK`.

**Termux.** Положить токен в файл секрета на DE. Бот не перезапускается:

```
ssh root@45.138.157.65 'cat /etc/vpn-db/secrets/db_gateway_token' | ssh de 'D=/root/secure-node-bot/node-rental/node-rental; (umask 077 && cat > "$D/secrets/db_gateway_token.txt.new") && chown 1000:1000 "$D/secrets/db_gateway_token.txt.new" && mv "$D/secrets/db_gateway_token.txt.new" "$D/secrets/db_gateway_token.txt" && wc -c < "$D/secrets/db_gateway_token.txt"'
```

Должно вывести `64`.

**Termux.** Сверить compose на DE. Копировать только если различия — две вставки про
`db_gateway_token`. Иначе прислать вывод и ничего не копировать:

```
ssh de 'diff /root/secure-node-bot/node-rental/node-rental/docker-compose.yml /root/vpn-src/docker-compose.yml'
```

Если различия в порядке, на DE:

```
ssh de 'cd /root/secure-node-bot/node-rental/node-rental && cp docker-compose.yml /root/docker-compose.before-secret.yml && cp /root/vpn-src/docker-compose.yml docker-compose.yml && docker compose -p node-rental config --quiet && echo COMPOSE_OK'
```

Должно закончиться строкой `COMPOSE_OK`. Откат файла: скопировать `/root/docker-compose.before-secret.yml`
обратно в `/root/secure-node-bot/node-rental/node-rental/docker-compose.yml`.

**Termux.** Переключить бота на секрет. Токен убирается из `.env`, бот перезапускается:

```
ssh root@45.138.157.65 'cat /etc/vpn-db/secrets/db_gateway_token' | ssh de 'bash /root/vpn-src/scripts/update_bot.sh --database-gateway http://10.204.0.1:8444'
```

Должно закончиться строкой `DATABASE_GATEWAY_OK`. Если бот не поднялся, скрипт сам вернёт
прежние `.env` и файл секрета; тогда прислать вывод.

**Termux.** Проверить на DE. Ожидается: в файле секрета `64`, в `.env` строк с токеном `0`,
в контейнере переменной `DB_GATEWAY_TOKEN` нет (`0`), а `/run/secrets` содержит `db_gateway_token`:

```
ssh de 'cd /root/secure-node-bot/node-rental/node-rental && wc -c < secrets/db_gateway_token.txt && grep -c "^DB_GATEWAY_TOKEN=" .env; cid=$(docker compose -p node-rental ps -q bot) && docker exec "$cid" sh -c "env | grep -c DB_GATEWAY_TOKEN; ls /run/secrets"'
```

**Termux.** Диагностика бота:

```
ssh de 'bash /root/vpn-src/scripts/update_bot.sh --doctor'
```

Итог без проблем, как было до переноса.
