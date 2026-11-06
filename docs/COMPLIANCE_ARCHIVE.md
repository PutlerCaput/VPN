# Архив подтверждённых блокировок

Текущий проект работает на SQLite/SQLCipher, таблицы PostgreSQL
`malicious_activity_logs` нет. `scripts/compliance_archiver.py` обслуживает
реальную схему: `anomalies.kind = 'AUTOBAN'` и `audit_log.action = 'autoban'`.
Платежи, аккаунты, неподтверждённые сигналы и другие записи не затрагиваются.
Это сведения об инциденте/блокировке, а не доказательство преступления.

Алгоритм фиксирует UTC-время начала и выбирает записи строго старше 30 суток.
JSON Lines читается по одной строке из согласованного снимка SQLite. Затем
создаётся tar.gz, поток шифруется GnuPG AES-256 с проверкой целостности; ключ
передаётся через отдельный файловый дескриптор, не через аргумент процесса.
Перед удалением архив полностью расшифровывается, SHA256 JSONL сравнивается с
экспортом, проверяется завершение GPG и сохраняется SHA256 шифротекста.
После fsync файла и каталога удаляются только строки, совпадающие по всем
исходным полям. Изменённые/добавленные конкурентно строки остаются в базе.

Удаление выполняется транзакциями до 250 строк. При ошибке до проверки
архива удаление не начинается. При ошибке во время очистки завершённые
пакеты уже находятся в сохранённом архиве; остальные остаются в БД.
Повторный запуск использует новый каталог, не перезаписывает старый архив.
`completed.json` появляется только после завершения очистки. Каталог без
него требует проверки; `.partial` не является подтверждённым архивом.

**Ежедневный запуск означает фактическое хранение примерно 30–31 сутки**,
плюс время выполнения/простоя. Он не обеспечивает удаление в точности в
момент достижения возраста 30 суток. Для строгого срока нужен более частый
планировщик и отдельный контроль задержки.

## Подготовка на DE

Нужны Python 3.11, `gpg` и установленное окружение проекта; для зашифрованной
БД — `sqlcipher3-binary` из requirements. Скрипт запускается на хосте,
не предполагает своего наличия в текущем работающем Docker-контейнере.

1. Перед первым запуском создайте проверенную резервную копию всей БД.
2. Подключите отдельный защищённый диск в `/mnt/cold-compliance`. Проверка:

   ```bash
   mountpoint /mnt/cold-compliance
   chmod 700 /mnt/cold-compliance
   ```

3. Создайте ключ и сохраните отдельную резервную копию в менеджере секретов
   владельца. Потеря ключа делает архивы невосстановимыми. Ключи БД также
   храните отдельно. Не отправляйте ключи в чат, Git или рядом с архивами в R2.

   ```bash
   install -d -m 700 /etc/node-rental /var/lib/node-compliance
   python3 - <<'PY'
   import os
   import secrets
   path = '/etc/node-rental/compliance-archive.key'
   fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
   with os.fdopen(fd, 'w') as output:
       output.write(secrets.token_hex(32) + '\n')
       output.flush()
       os.fsync(output.fileno())
   print('Ключ создан; содержимое не выводилось.')
   PY
   ```

4. Путь текущего production-проекта по прежнему выводу владельца:
   `/root/secure-node-bot/node-rental/node-rental`. Уточните его до запуска.
   Не копируйте активный файл SQLite обычным `cp`; используйте online backup.
   Ручной пример после размещения новой версии в `/opt/node-rental`:

   ```bash
   /opt/node-rental/.venv/bin/python /opt/node-rental/scripts/compliance_archiver.py \
     --database /root/secure-node-bot/node-rental/node-rental/data/bot.db \
     --db-key-file /root/secure-node-bot/node-rental/node-rental/secrets/db_key.txt \
     --archive-key-file /etc/node-rental/compliance-archive.key \
     --cold-dir /mnt/cold-compliance \
     --temp-dir /var/lib/node-compliance
   ```

   Для незашифрованной БД уберите `--db-key-file`; неверный ключ не приводит
   к переходу на незашифрованный режим. Вместо файлов допускаются переменные
   `DB_KEY` и `COMPLIANCE_ARCHIVE_KEY`. Файлы секретов должны иметь режим 600.

## Таймер, дисковое место и оповещения

В `deploy/compliance/` находятся systemd service/timer. Сначала адаптируйте
пути к БД/ключам и `ReadWritePaths`. Шаблон рассчитан на `/opt/node-rental`;
при работе с прежним `/root/...` нужен явный доступ к этому каталогу вместо
`ProtectHome=true`. Не включайте неподготовленный шаблон на production.
После сверки:

```bash
systemd-analyze verify deploy/compliance/node-compliance-archive.service deploy/compliance/node-compliance-archive.timer
install -m 644 deploy/compliance/node-compliance-archive.service /etc/systemd/system/
install -m 644 deploy/compliance/node-compliance-archive.timer /etc/systemd/system/
systemctl daemon-reload
systemctl start node-compliance-archive.service
systemctl enable --now node-compliance-archive.timer
```

Запуск — ежедневно 03:24 **Asia/Novosibirsk**. Сначала вручную убедитесь, что
архив расшифровывается и счётчики строк ожидаемые. Нужны свободное место под
временный JSONL и шифрованный архив и запас под WAL. Временный JSONL находится
в каталоге 700, файл 600; он удаляется при обычном завершении. На SSD удаление
файла не доказывает физического стирания: используйте шифрованный временный
том и шифрование swap; после аварийного убийства проверьте оставшиеся
`compliance-*` каталоги в `/var/lib/node-compliance`.

Опционально файл `/etc/node-rental/compliance-alert.env` (600) содержит
`COMPLIANCE_ALERT_WEBHOOK=https://...` — адрес вашего обработчика оповещений.
На ошибке отправляется только JSON
`{"priority":"high","event":"compliance_archive_failed"}`. Это общий webhook;
для Telegram нужен ваш обработчик, преобразующий событие в sendMessage.
Если webhook не настроен или недоступен, ошибка остаётся в journald; контроль
неуспешных systemd units должен быть настроен отдельно.

После DELETE выполняются `secure_delete=ON`, `wal_checkpoint(PASSIVE)` и
`PRAGMA optimize`. Освобождённые страницы могут повторно использоваться SQLite,
но **возврат SSD-блоков ОС и полное физическое стирание не гарантируются**.
`VACUUM` требует блокировки записи и свободного диска; автоматически он не
запускается. Для PostgreSQL нужна отдельная миграция/архитектура с партициями.

Каталоги имеют вид `YYYY_MM_DD_<uuid>/compliance_archive_YYYY_MM_DD.tar.gz.enc`.
В R2 передавайте только архив и его контрольную сумму через проверенный
upload-скрипт. Ключи остаются отдельно. Этот архиватор подтверждает локальную
холодную копию перед очисткой; удалённая R2-копия не является условием DELETE.
Для обязательного подтверждения именно R2 перед удалением нужна дополнительная
интеграция, поэтому размещайте cold-dir на действительно отдельном диске.
