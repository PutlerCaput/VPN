#!/usr/bin/env bash
# One line for the doctor: is the weekly cleanup (scripts/install_cleanup.sh)
# set up, and has it finished lately? Prints "OK <text>" or "WARN <text>".
# Changes nothing.
set -uo pipefail

UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
STATE_DIR="${STATE_DIR:-/var/lib/node-cleanup}"
MAX_AGE_DAYS="${MAX_AGE_DAYS:-10}"
STATE="$STATE_DIR/last-run"

if [[ ! -f "$UNIT_DIR/node-cleanup.timer" ]]; then
  echo "WARN no weekly cleanup: bash scripts/install_cleanup.sh (docs/SERVER_MAINTENANCE.md)"
  exit 0
fi
if command -v systemctl >/dev/null 2>&1 && ! systemctl is-active --quiet node-cleanup.timer; then
  echo "WARN the weekly cleanup timer is not active: systemctl enable --now node-cleanup.timer"
  exit 0
fi
if [[ ! -f "$STATE" ]]; then
  echo "WARN the weekly cleanup is set up but has not run yet: systemctl start node-cleanup.service"
  exit 0
fi

get() { sed -n "s/^$1=//p" "$STATE" | head -n1; }
when="$(get time)"
last_ok="$(get last_ok)"
result="$(get result)"
freed="$(get freed_mb)"
free="$(get free_mb)"
note="$(get note)"
if ! [[ "$when" =~ ^[0-9]+$ ]]; then
  echo "WARN the record of the weekly cleanup is unreadable: $STATE"
  exit 0
fi

now="$(date +%s)"
days=$(((now - when) / 86400))
since="it has never completed"
ok_days=-1
if [[ "$last_ok" =~ ^[0-9]+$ ]]; then
  ok_days=$(((now - last_ok) / 86400))
  since="the last complete run was $ok_days d ago"
fi

case "$result" in
  failed)
    echo "WARN the weekly cleanup failed $days d ago: $note (journalctl -u node-cleanup.service)"
    ;;
  skipped)
    if ((ok_days < 0 || ok_days > MAX_AGE_DAYS)); then
      echo "WARN the weekly cleanup was skipped $days d ago ($note) and $since: systemctl status node-cleanup.service"
    else
      echo "OK weekly cleanup: skipped $days d ago ($note); $since; it tries again next week"
    fi
    ;;
  ok)
    if ((days > MAX_AGE_DAYS)); then
      echo "WARN the weekly cleanup has not run for $days d: systemctl list-timers node-cleanup.timer"
    else
      gb="$(awk -v m="${free:-0}" 'BEGIN { printf "%.1f", m / 1024 }')"
      echo "OK weekly cleanup: ran $days d ago, freed ${freed:-0} MB, $gb GB free"
    fi
    ;;
  *)
    echo "WARN the record of the weekly cleanup is unreadable: $STATE"
    ;;
esac
