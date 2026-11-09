#!/usr/bin/env bash
# Turn on the monthly safe update (scripts/server_auto_update.sh) with a systemd
# timer.
#
#   bash scripts/install_auto_update.sh            # first Sunday of the month, a short note to the owner in Telegram
#   bash scripts/install_auto_update.sh --quiet    # same, but a note only when something is wrong
#   bash scripts/install_auto_update.sh --remove   # turn it off
#
# It needs the daily backup (scripts/install_backup.sh): without a fresh copy
# the update does not start. What it updates, what it never touches and when it
# waits: docs/SERVER_MAINTENANCE.md. Writes a service and a timer (the first
# Sunday of each month at 05:00 UTC, after the cleanup, with a random delay of up
# to 10 minutes) and shows what the update would do right now, changing nothing.
# A month the server was off is skipped, not made up at an odd hour.
set -euo pipefail

SRC="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
fail() { printf 'AUTO_UPDATE_INSTALL_FAILED: %s\n' "$*" >&2; exit 1; }
usage() { fail "usage: install_auto_update.sh [--quiet | --remove]"; }

[[ $# -le 1 ]] || usage
NOTIFY=always
REMOVE=0
case "${1:-}" in
  "") ;;
  --quiet) NOTIFY=problems ;;
  --remove) REMOVE=1 ;;
  *) usage ;;
esac

# An ETC_DIR given by the caller is a sandbox (tests, a dry run), not the server.
if [[ -z "${ETC_DIR:-}" ]]; then
  [[ $EUID -eq 0 ]] || fail "run as root"
  ETC_DIR=/etc
fi
UNIT_DIR="${UNIT_DIR:-$ETC_DIR/systemd/system}"
# The folder is written into a unit file: nothing but a plain path.
[[ "$SRC" =~ ^/[A-Za-z0-9._/-]+$ ]] || fail "the project folder must have a plain path (letters, digits, . _ - /): $SRC"
[[ -f "$SRC/scripts/server_auto_update.sh" ]] || fail "run from the project checkout"

if ((REMOVE)); then
  systemctl disable --now node-update.timer >/dev/null 2>&1 || true
  rm -f "$UNIT_DIR/node-update.service" "$UNIT_DIR/node-update.timer"
  systemctl daemon-reload
  printf 'AUTO_UPDATE_REMOVED: the monthly update is off. The supervised one still works: bash scripts/server_update.sh\n'
  exit 0
fi

[[ -f "$ETC_DIR/node-backup.env" ]] \
  || fail "set up the daily backup first (bash scripts/install_backup.sh <age public key>): the update never starts without a fresh copy"

mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/node-update.service" <<EOF
[Unit]
Description=Monthly safe update of the server (system packages, base images)
After=docker.service network-online.target
Wants=docker.service network-online.target

[Service]
Type=oneshot
Environment=NOTIFY=$NOTIFY
ExecStart=/bin/bash $SRC/scripts/server_auto_update.sh
Nice=10
TimeoutStartSec=2h
EOF
cat > "$UNIT_DIR/node-update.timer" <<EOF
[Unit]
Description=Monthly safe update of the server

[Timer]
OnCalendar=Sun *-*-1..7 05:00:00 UTC
RandomizedDelaySec=10min
Persistent=false

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now node-update.timer >/dev/null

printf 'timer: %s\n\n' "$(systemctl list-timers node-update.timer --no-pager 2>/dev/null | sed -n 2p)"
printf 'what the update would do right now (nothing is changed):\n\n'
check=0
bash "$SRC/scripts/server_auto_update.sh" --dry-run || check=$?
printf '\n'
if ((check)); then
  printf 'AUTO_UPDATE_INSTALL_DONE_CHECK_FAILED: the timer is on, but the trial run stopped (see above).\n' >&2
  exit 1
fi
printf 'AUTO_UPDATE_INSTALL_OK. It runs on the first Sunday of each month at about 05:00 UTC.\n'
printf 'Docker, Marzban, the project code and the reboot stay with you (docs/SERVER_MAINTENANCE.md).\n'
printf 'Check: bash scripts/update_bot.sh --doctor   Off: bash scripts/install_auto_update.sh --remove\n'
