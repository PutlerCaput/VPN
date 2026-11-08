#!/usr/bin/env bash
# Turn on the daily encrypted backup (scripts/backup_all.sh) with a systemd timer.
#
#   bash scripts/install_backup.sh age1…         # the owner's PUBLIC key
#
# The key is made on the owner's phone or computer, never here:
#   age-keygen -o ~/age-key.txt      prints "Public key: age1…"; the file is the
#                                    private key: keep two copies off the server.
# Writes /etc/node-backup.env, a service and a timer (03:20 UTC every day, with a
# random delay of up to 10 minutes), installs age if it is missing, and makes
# the first backup at once so the owner sees it arrive in Telegram.
set -euo pipefail

KEY="${1:-}"
SRC="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
ETC_DIR="${ETC_DIR:-/etc}"
UNIT_DIR="${UNIT_DIR:-$ETC_DIR/systemd/system}"

fail() { printf 'BACKUP_INSTALL_FAILED: %s\n' "$*" >&2; exit 1; }

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
BACKUP_DIR=/root/server-backups
KEEP_LOCAL=7
SEND_TELEGRAM=1
EOF
umask 022
cat > "$UNIT_DIR/node-backup.service" <<EOF
[Unit]
Description=Encrypted backup of the bot, Marzban and their settings
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
printf 'Check: bash scripts/backup_status.sh   Restore: docs/BACKUPS.md\n'
