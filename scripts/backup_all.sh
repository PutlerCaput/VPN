#!/usr/bin/env bash
# Encrypted backup of everything that cannot be re-created, sent to the owner.
#
#   bash scripts/backup_all.sh        # one backup now (a daily timer runs it)
#
# Packs into ONE archive:
#   bot/bot.db          the bot's database (online copy, still SQLCipher-encrypted;
#                       fetched from the other server when the bot keeps it there)
#   bot/.env, secrets/  the bot's settings and keys (the database key among them)
#   hiddify/            Hiddify Manager's database, .env, compose file, Caddyfile, Xray config
# encrypts it to the owner's PUBLIC key with age, keeps the newest copies in
# /root/server-backups and sends the new one to the owner in Telegram, so a copy
# lives off this server. The private key is never on the server: without it the
# archive cannot be opened, here or in Telegram. Settings: /etc/node-backup.env,
# written by install_backup.sh.
#
# Not included: Hiddify Manager's geo files (downloaded again), Docker images (rebuilt
# from the repository) and the AmneziaWG server key (see docs/BACKUPS.md).
set -euo pipefail
umask 077

CONF="${BACKUP_ENV:-/etc/node-backup.env}"
if [[ -f "$CONF" ]]; then
  # shellcheck disable=SC1090
  . "$CONF"
fi
AGE_RECIPIENT="${AGE_RECIPIENT:-}"
BACKUP_DIR="${BACKUP_DIR:-/root/server-backups}"
KEEP_LOCAL="${KEEP_LOCAL:-7}"
CONTAINER="${CONTAINER:-node-rental-bot-1}"
HIDDIFY_DB="${HIDDIFY_DB:-/var/lib/hiddify/db.sqlite3}"
HIDDIFY_DIR="${HIDDIFY_DIR:-/opt/hiddify-manager}"
XRAY_CONFIG="${XRAY_CONFIG:-/var/lib/hiddify/xray_config.json}"
SEND_TELEGRAM="${SEND_TELEGRAM:-1}"
SRC="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
WARNINGS=()

say() { printf '%s\n' "$*"; }
bot_text() {  # best effort: a note to the owner from the bot
  docker exec "$CONTAINER" python scripts/send_backup.py text "$1" >/dev/null 2>&1 || true
}
fail() {
  printf 'BACKUP_FAILED: %s\n' "$*" >&2
  bot_text "⚠️ Резервная копия не сделана: $*"
  exit 1
}

[[ "$AGE_RECIPIENT" =~ ^age1[0-9a-z]{58}$ ]] \
  || fail "AGE_RECIPIENT is missing or is not an age public key (age1…); see docs/BACKUPS.md"
command -v age >/dev/null 2>&1 || fail "age is not installed (apt-get install age)"
docker inspect "$CONTAINER" >/dev/null 2>&1 || fail "container $CONTAINER not found"
WORKDIR="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$CONTAINER")"
[[ -n "$WORKDIR" && -d "$WORKDIR" ]] || fail "the bot's project folder was not found"

# The timer and a manual run share the files mounted into the bot container.
# Lock that directory even if the two invocations use different backup folders.
exec 9<"$WORKDIR/data" || fail "the bot's data folder could not be opened"
flock -n 9 || fail "could not take the backup lock; another backup may be running"

STAMP="$(date -u +%Y%m%dT%H%M%S.%NZ)"
mkdir -p "$BACKUP_DIR"
OUT="$BACKUP_DIR/backup-$STAMP.tar.gz.age"
[[ ! -e "$OUT" ]] || fail "an archive already exists for this timestamp"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"; rm -f "$OUT.partial" "$WORKDIR/data/.nightly-backup.db" "$WORKDIR/data/.outgoing-backup.age"' EXIT
mkdir -p "$TMP/bot" "$TMP/hiddify"

# ----- the bot: database, settings, keys
if grep -q '^DATABASE_URL=.' "$WORKDIR/.env" 2>/dev/null; then
  # The database is on the other server: the file here is an old copy. Take the
  # live one through the gateway (docs/RU_DATABASE_PLAN.md, section 11).
  docker exec "$CONTAINER" python scripts/db_tool.py fetch /app/data/.nightly-backup.db >/dev/null \
    || fail "the bot database could not be fetched from the other server"
else
  docker exec "$CONTAINER" python scripts/db_tool.py backup /app/data/bot.db /app/data/.nightly-backup.db >/dev/null \
    || fail "the bot database could not be copied"
fi
[[ -s "$WORKDIR/data/.nightly-backup.db" ]] || fail "the bot database copy is empty"
mv "$WORKDIR/data/.nightly-backup.db" "$TMP/bot/bot.db"
[[ -f "$WORKDIR/.env" ]] && cp -p "$WORKDIR/.env" "$TMP/bot/.env"
[[ -d "$WORKDIR/secrets" ]] && cp -rp "$WORKDIR/secrets" "$TMP/bot/secrets"

# ----- Hiddify Manager: database, settings
if [[ -f "$HIDDIFY_DB" ]]; then
  python3 - "$HIDDIFY_DB" "$TMP/hiddify/db.sqlite3" <<'PY' || fail "the Hiddify Manager database could not be copied"
import sqlite3
import sys

try:
    source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
    source.execute("SELECT 1 FROM sqlite_master LIMIT 1")
except sqlite3.OperationalError:
    source = sqlite3.connect(sys.argv[1], timeout=30)  # backup() only reads
target = sqlite3.connect(sys.argv[2])
with target:
    source.backup(target)
check = target.execute("PRAGMA integrity_check").fetchone()[0]
target.close()
source.close()
if check != "ok":
    sys.exit(f"integrity_check: {check}")
PY
else
  WARNINGS+=("нет базы Hiddify Manager ($HIDDIFY_DB)")
fi
for file in "$HIDDIFY_DIR/.env" "$HIDDIFY_DIR/docker-compose.yml" "$HIDDIFY_DIR/Caddyfile" "$XRAY_CONFIG"; do
  if [[ -f "$file" ]]; then cp -p "$file" "$TMP/hiddify/"; else WARNINGS+=("нет файла $file"); fi
done

# ----- what is inside, and what could not be added
{
  printf 'made: %s UTC on %s\n' "$STAMP" "$(hostname)"
  printf 'source: %s\n' "$(git -C "$SRC" log -1 --format='%h %s' 2>/dev/null || echo unknown)"
  for item in "${WARNINGS[@]+"${WARNINGS[@]}"}"; do printf 'WARNING: %s\n' "$item"; done
} > "$TMP/INFO.txt"
(cd "$TMP" && find . -type f ! -path './MANIFEST.sha256' -print0 | sort -z | xargs -0 sha256sum) > "$TMP/MANIFEST.sha256"

# ----- pack, encrypt, keep the newest
tar -C "$TMP" -czf - . | age -r "$AGE_RECIPIENT" -o "$OUT.partial" || fail "the archive could not be encrypted"
[[ -s "$OUT.partial" ]] || fail "the encrypted archive is empty"
mv "$OUT.partial" "$OUT"
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'backup-*.tar.gz.age' -printf '%T@ %p\n' \
  | sort -rn | cut -d' ' -f2- | {
    n=0
    while IFS= read -r old; do
      n=$((n + 1))
      if ((n > KEEP_LOCAL)); then rm -f -- "$old"; fi
    done
  }
SIZE="$(du -k "$OUT" | cut -f1)"

# ----- a copy off this server
SENT=skipped
if [[ "$SEND_TELEGRAM" == 1 ]]; then
  cp "$OUT" "$WORKDIR/data/.outgoing-backup.age"
  # The bot runs as uid 1000 and must be able to read it; the archive is encrypted.
  chmod 644 "$WORKDIR/data/.outgoing-backup.age"
  caption="💾 Резервная копия $STAMP, ${SIZE} КБ. Откроет только ваш закрытый ключ age."
  if ((${#WARNINGS[@]})); then caption="$caption ⚠️ ${WARNINGS[*]}"; fi
  if docker exec "$CONTAINER" python scripts/send_backup.py file /app/data/.outgoing-backup.age "$caption" >/dev/null 2>&1; then
    SENT=sent
    touch "$BACKUP_DIR/.last-sent"
  else
    SENT=failed
    WARNINGS+=("копию не удалось отправить в Telegram")
  fi
fi

say "BACKUP_OK $OUT ${SIZE}KB telegram=$SENT"
for item in "${WARNINGS[@]+"${WARNINGS[@]}"}"; do say "  warning: $item"; done
