#!/usr/bin/env bash
# One line for the doctor: is there a fresh encrypted backup, and did a copy
# leave the server? Prints "OK <text>" or "WARN <text>". Changes nothing.
set -uo pipefail

CONF="${BACKUP_ENV:-/etc/node-backup.env}"
if [[ -f "$CONF" ]]; then
  # shellcheck disable=SC1090
  . "$CONF"
fi
BACKUP_DIR="${BACKUP_DIR:-/root/server-backups}"
SEND_TELEGRAM="${SEND_TELEGRAM:-1}"
MAX_AGE_HOURS="${MAX_AGE_HOURS:-36}"
MAX_SENT_DAYS="${MAX_SENT_DAYS:-3}"

if [[ ! -f "$CONF" ]]; then
  echo "WARN no scheduled backup: bash scripts/install_backup.sh <age public key> (docs/BACKUPS.md)"
  exit 0
fi
newest="$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'backup-*.tar.gz.age' -printf '%T@\n' 2>/dev/null | sort -rn | head -n1)"
if [[ -z "$newest" ]]; then
  echo "WARN backup is set up but no archive exists: bash scripts/backup_all.sh; systemctl status node-backup.service"
  exit 0
fi
hours=$((($(date +%s) - ${newest%.*}) / 3600))
if ((hours > MAX_AGE_HOURS)); then
  echo "WARN the newest backup is $hours h old: systemctl status node-backup.timer node-backup.service"
  exit 0
fi
if [[ "$SEND_TELEGRAM" == 1 ]]; then
  sent="$(find "$BACKUP_DIR" -maxdepth 1 -name .last-sent -printf '%T@\n' 2>/dev/null | head -n1)"
  if [[ -z "$sent" ]] || (($(date +%s) - ${sent%.*} > MAX_SENT_DAYS * 86400)); then
    echo "WARN the newest backup is $hours h old, but no copy reached Telegram in $MAX_SENT_DAYS days: bash scripts/backup_all.sh"
    exit 0
  fi
fi
echo "OK backup: the newest is $hours h old, a copy is off the server"
