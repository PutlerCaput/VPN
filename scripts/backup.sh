#!/usr/bin/env bash
# Encrypted backup: online backup (SQLCipher-encrypted when db_key is set) -> GPG -> ./backups.
# Cron: 0 4 * * * /opt/node-bot/scripts/backup.sh >> /opt/node-bot/logs/backup.log 2>&1
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  set -a; source <(grep -E '^(DATABASE_PATH|BACKUP_GPG_RECIPIENT|BACKUP_RETENTION_DAYS)=' .env); set +a
fi

DB_PATH="${DATABASE_PATH:-data/bot.db}"
RECIPIENT="${BACKUP_GPG_RECIPIENT:?set BACKUP_GPG_RECIPIENT (GPG key id or email)}"
RETENTION="${BACKUP_RETENTION_DAYS:-30}"
OUT_DIR="$ROOT/backups"
STAMP="$(date -u +%Y%m%d)"
OUT="$OUT_DIR/bot-${STAMP}.db.gpg"

umask 077
mkdir -p "$OUT_DIR"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [[ -s secrets/db_key.txt ]]; then
  # SQLCipher: the online backup runs inside the bot container, which has the
  # key and the sqlcipher module; the copy stays encrypted with the same key.
  docker compose exec -T bot python scripts/db_tool.py backup /app/data/bot.db /app/data/.backup.db
  mv data/.backup.db "$TMP/bot.db"
else
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB_PATH" ".backup '$TMP/bot.db'"
  else
    python3 - "$DB_PATH" "$TMP/bot.db" <<'PY'
import sqlite3
import sys

src = sqlite3.connect(sys.argv[1])
dst = sqlite3.connect(sys.argv[2])
with dst:
    src.backup(dst)
dst.close()
src.close()
PY
  fi
  python3 - "$TMP/bot.db" <<'PY'
import sqlite3
import sys

result = sqlite3.connect(sys.argv[1]).execute("PRAGMA integrity_check").fetchone()[0]
if result != "ok":
    sys.exit(f"integrity_check failed: {result}")
PY
fi

gpg --batch --yes --trust-model always --encrypt --recipient "$RECIPIENT" \
    --output "$OUT" "$TMP/bot.db"
find "$OUT_DIR" -name 'bot-*.db.gpg' -mtime +"$RETENTION" -delete
echo "✅ Backup written: $OUT ($(du -h "$OUT" | cut -f1))"
