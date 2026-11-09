#!/usr/bin/env bash
# Turn on the weekly safe cleanup (scripts/server_cleanup.sh --auto) with a
# systemd timer.
#
#   bash scripts/install_cleanup.sh            # every Sunday, a short note to the owner in Telegram
#   bash scripts/install_cleanup.sh --quiet    # same, but a note only when something is wrong
#   bash scripts/install_cleanup.sh --remove   # turn it off
#
# The weekly run removes only what an update has already replaced and nothing
# younger than 7 days, stops while an update, a backup or a package upgrade is
# running or any container is not healthy, and checks afterwards that the same
# containers still run (what exactly: docs/SERVER_MAINTENANCE.md). See what it
# would do right now, without removing anything:
#   bash scripts/server_cleanup.sh --auto --dry-run
# Writes a service and a timer (Sunday 04:30 UTC, after the daily backup, with a
# random delay of up to 15 minutes) and makes the first pass at once.
set -euo pipefail

SRC="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
fail() { printf 'CLEANUP_INSTALL_FAILED: %s\n' "$*" >&2; exit 1; }
usage() { fail "usage: install_cleanup.sh [--quiet | --remove]"; }

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
[[ -f "$SRC/scripts/server_cleanup.sh" ]] || fail "run from the project checkout"

if ((REMOVE)); then
  systemctl disable --now node-cleanup.timer >/dev/null 2>&1 || true
  rm -f "$UNIT_DIR/node-cleanup.service" "$UNIT_DIR/node-cleanup.timer"
  systemctl daemon-reload
  printf 'CLEANUP_REMOVED: the weekly cleanup is off. The manual one still works: bash scripts/server_cleanup.sh\n'
  exit 0
fi

mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/node-cleanup.service" <<EOF
[Unit]
Description=Weekly safe cleanup of old images, build cache and copies
After=docker.service
Wants=docker.service

[Service]
Type=oneshot
Environment=NOTIFY=$NOTIFY
ExecStart=/bin/bash $SRC/scripts/server_cleanup.sh --auto
Nice=19
IOSchedulingClass=idle
TimeoutStartSec=30min
EOF
cat > "$UNIT_DIR/node-cleanup.timer" <<EOF
[Unit]
Description=Weekly safe cleanup

[Timer]
OnCalendar=Sun *-*-* 04:30:00 UTC
RandomizedDelaySec=15min
Persistent=true

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now node-cleanup.timer >/dev/null

printf 'timer: %s\n' "$(systemctl list-timers node-cleanup.timer --no-pager 2>/dev/null | sed -n 2p)"
printf 'first pass now...\n\n'
first=0
NOTIFY="$NOTIFY" bash "$SRC/scripts/server_cleanup.sh" --auto || first=$?
printf '\n'
if ((first)); then
  printf 'CLEANUP_INSTALL_DONE_FIRST_PASS_FAILED: the timer is on, but the first pass stopped (see above).\n' >&2
  exit 1
fi
printf 'CLEANUP_INSTALL_OK. It runs every Sunday at about 04:30 UTC.\n'
printf 'Check: bash scripts/update_bot.sh --doctor   Off: bash scripts/install_cleanup.sh --remove\n'
