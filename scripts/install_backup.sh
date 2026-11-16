#!/usr/bin/env bash
# Turn on the daily encrypted backup (scripts/backup_all.sh) with a systemd timer.
#
#   bash scripts/install_backup.sh age1…                  # the owner's PUBLIC key
#   bash scripts/install_backup.sh age1… --no-telegram    # same, but no copy leaves this server
#
# The key is made on the owner's phone or computer, never here:
#   age-keygen -o ~/age-key.txt      prints "Public key: age1…"; the file is the
#                                    private key: keep two copies off the server.
# Writes /etc/node-backup.env, a service and a timer (03:20 UTC every day, with a
# random delay of up to 10 minutes), installs age if it is missing, and makes
# the first backup at once so the owner sees it arrive in Telegram. With
# --no-telegram the copies stay here (the newest 7) and nothing is sent; fetch
# them to your own device with scp (docs/BACKUPS.md).
set -euo pipefail

KEY="${1:-}"
SRC="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
ETC_DIR="${ETC_DIR:-/etc}"
UNIT_DIR="${UNIT_DIR:-$ETC_DIR/systemd/system}"
BACKUP_DIR="${BACKUP_DIR:-/root/server-backups}"

fail() { printf 'BACKUP_INSTALL_FAILED: %s\n' "$*" >&2; exit 1; }

SEND_TELEGRAM=1
case "${2:-}" in
  "") ;;
  --no-telegram) SEND_TELEGRAM=0 ;;
  *) fail "usage: install_backup.sh age1… [--no-telegram]" ;;
esac
[[ $# -le 2 ]] || fail "usage: install_backup.sh age1… [--no-telegram]"
# The folder is written into a file that bash sources: nothing but a plain path.
[[ "$BACKUP_DIR" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "BACKUP_DIR must be a plain absolute path"

[[ "$KEY" =~ ^age1[0-9a-z]{58}$ ]] \
  || fail "usage: install_backup.sh age1… (the PUBLIC key; it starts with age1, the private one with AGE-SECRET-KEY)"
[[ $EUID -eq 0 ]] || fail "run as root"
[[ -f "$SRC/scripts/backup_all.sh" ]] || fail "run from the project checkout"

if ! command -v age >/dev/null 2>&1; then
  apt-get install -y -q age >/dev/null 2>&1 || fail "age could not be installed: apt-get install age"
fi

mkdir -p "$UNIT_DIR"
umask 077
cat > "$ETC_DIR/node-backup.env" <<EOF
# Written by install_backup.sh. The key is public: it can only lock archives.
AGE_RECIPIENT=$KEY
BACKUP_DIR=$BACKUP_DIR
KEEP_LOCAL=7
SEND_TELEGRAM=$SEND_TELEGRAM
EOF
umask 022
cat > "$UNIT_DIR/node-backup.service" <<EOF
[Unit]
Description=Encrypted backup of the bot, Hiddify Manager and their settings
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
ExecStart=/bin/bash $SRC/scripts/backup_all.sh
Nice=10
IOSchedulingClass=idle
TimeoutStartSec=20min
EOF
cat > "$UNIT_DIR/node-backup.timer" <<EOF
[Unit]
Description=Daily encrypted backup

[Timer]
OnCalendar=*-*-* 03:20:00 UTC
RandomizedDelaySec=10min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now node-backup.timer >/dev/null

printf 'timer: %s\n' "$(systemctl list-timers node-backup.timer --no-pager 2>/dev/null | sed -n 2p)"
printf 'first backup now...\n'
BACKUP_ENV="$ETC_DIR/node-backup.env" bash "$SRC/scripts/backup_all.sh"
printf '\nBACKUP_INSTALL_OK. Keep the private key (AGE-SECRET-KEY-…) off this server, in two places.\n'
if [[ "$SEND_TELEGRAM" == 0 ]]; then
  printf 'No copy leaves this server: fetch the newest one to your own device every few days (docs/BACKUPS.md).\n'
fi
printf 'Check: bash scripts/backup_status.sh   Restore: docs/BACKUPS.md\n'
