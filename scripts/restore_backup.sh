#!/usr/bin/env bash
# Open a backup made by backup_all.sh into a NEW folder and check it. Touches
# nothing that is running: it only unpacks and prints what to copy where.
#
#   bash scripts/restore_backup.sh backup-….tar.gz.age age-key.txt [folder]
#
# age-key.txt is the owner's private key (AGE-SECRET-KEY-…). The folder must not
# exist yet; the default is ./restore-<time>.
#
# On some phones Go programs do not run in Termux: every option is taken for a
# stray argument, even `age --version`. Then rage (the same format, written in
# Rust, `pkg install rage`) opens the archive instead.
set -euo pipefail
umask 077

ARCHIVE="${1:-}"
IDENTITY="${2:-}"
DEST="${3:-./restore-$(date -u +%Y%m%dT%H%M%SZ)}"

fail() { printf 'RESTORE_FAILED: %s\n' "$*" >&2; exit 1; }

[[ -f "$ARCHIVE" && -f "$IDENTITY" ]] || fail "usage: restore_backup.sh <backup.tar.gz.age> <age-key.txt> [folder]"
if age --version >/dev/null 2>&1; then
  OPEN=age
elif command -v rage >/dev/null 2>&1; then
  OPEN=rage
else
  fail "age is not installed or does not run (apt-get install age; in Termux: pkg install rage)"
fi
[[ ! -e "$DEST" ]] || fail "$DEST already exists: choose a new folder"

mkdir -p "$DEST"
if ! "$OPEN" -d -i "$IDENTITY" "$ARCHIVE" | tar -xz -C "$DEST"; then
  rm -rf "$DEST"
  fail "the archive could not be opened: wrong key, or the file is damaged"
fi
(cd "$DEST" && sha256sum --quiet -c MANIFEST.sha256) || fail "the contents do not match MANIFEST.sha256: the archive is damaged"

printf 'RESTORE_OK in %s\n\n' "$DEST"
cat "$DEST/INFO.txt"
printf '\nContents:\n'
(cd "$DEST" && find . -type f ! -name MANIFEST.sha256 ! -name INFO.txt | sort | sed 's/^\.\//  /')
cat <<EOF

To bring a server back (new or repaired), as root, in this order. Nothing here
was run for you.
  1. The bot's folder (where docker-compose.yml is), bot stopped:
       cp $DEST/bot/.env  <folder>/.env
       cp -rp $DEST/bot/secrets/. <folder>/secrets/
       cp $DEST/bot/bot.db <folder>/data/bot.db && chown 1000:1000 <folder>/data/bot.db
     then start it: docker compose up -d   (docs/FINISH_RUNBOOK.md, step 2)
  2. Hiddify Manager, stopped:
       cp $DEST/hiddify/db.sqlite3 /opt/hiddify-manager/hiddify.db
       cp $DEST/hiddify/.env $DEST/hiddify/docker-compose.yml $DEST/hiddify/Caddyfile /opt/hiddify-manager/
       cp $DEST/hiddify/xray_config.json /opt/hiddify-manager/
     then start the panel again
  3. Check: bash scripts/update_bot.sh --doctor
Delete $DEST afterwards: it holds the keys in the clear.
EOF
