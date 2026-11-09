#!/usr/bin/env bash
# One line for the doctor: is the monthly update (scripts/install_auto_update.sh)
# set up, has it finished lately, and is a reboot waiting? Prints "OK <text>" or
# "WARN <text>". Changes nothing.
set -uo pipefail

UNIT_DIR="${UNIT_DIR:-/etc/systemd/system}"
STATE_DIR="${STATE_DIR:-/var/lib/node-update}"
REBOOT_FILE="${REBOOT_FILE:-/var/run/reboot-required}"
MAX_AGE_DAYS="${MAX_AGE_DAYS:-45}"
REBOOT_WARN_DAYS="${REBOOT_WARN_DAYS:-14}"
STATE="$STATE_DIR/last-run"
TIMER="$UNIT_DIR/node-update.timer"
now="$(date +%s)"

# A new kernel or library only works after a reboot, with or without the timer.
reboot_text=""
if [[ -f "$REBOOT_FILE" ]]; then
  waiting=$(((now - $(stat -c %Y "$REBOOT_FILE" 2>/dev/null || echo "$now")) / 86400))
  if ((waiting > REBOOT_WARN_DAYS)); then
    echo "WARN a reboot has been waiting for $waiting d (new kernel or libraries): run reboot at a quiet hour, the containers come back by themselves"
    exit 0
  fi
  reboot_text="; a reboot is waiting ($waiting d)"
fi

if [[ ! -f "$TIMER" ]]; then
  echo "WARN no monthly update: bash scripts/install_auto_update.sh (docs/SERVER_MAINTENANCE.md)$reboot_text"
  exit 0
fi
if command -v systemctl >/dev/null 2>&1 && ! systemctl is-active --quiet node-update.timer; then
  echo "WARN the monthly update timer is not active: systemctl enable --now node-update.timer"
  exit 0
fi
if [[ ! -f "$STATE" ]]; then
  age=$(((now - $(stat -c %Y "$TIMER" 2>/dev/null || echo "$now")) / 86400))
  if ((age > 40)); then
    echo "WARN the monthly update is set up but has never run: systemctl list-timers node-update.timer"
  else
    echo "OK monthly update: set up, waiting for the first Sunday of the month$reboot_text"
  fi
  exit 0
fi

get() { sed -n "s/^$1=//p" "$STATE" | head -n1; }
when="$(get time)"
last_ok="$(get last_ok)"
result="$(get result)"
packages="$(get packages)"
images="$(get images)"
note="$(get note)"
if ! [[ "$when" =~ ^[0-9]+$ ]]; then
  echo "WARN the record of the monthly update is unreadable: $STATE"
  exit 0
fi

days=$(((now - when) / 86400))
since="it has never completed"
ok_days=-1
if [[ "$last_ok" =~ ^[0-9]+$ ]]; then
  ok_days=$(((now - last_ok) / 86400))
  since="the last complete run was $ok_days d ago"
fi

case "$result" in
  failed)
    echo "WARN the monthly update failed $days d ago: $note (journalctl -u node-update.service)"
    ;;
  skipped)
    if ((ok_days < 0 || ok_days > MAX_AGE_DAYS)); then
      echo "WARN the monthly update was skipped $days d ago ($note) and $since: systemctl status node-update.service"
    else
      echo "OK monthly update: skipped $days d ago ($note); $since; it tries again next month$reboot_text"
    fi
    ;;
  ok)
    if ((days > MAX_AGE_DAYS)); then
      echo "WARN the monthly update has not run for $days d: systemctl list-timers node-update.timer"
    else
      echo "OK monthly update: ran $days d ago, ${packages:-0} package(s), images ${images:-unknown}$reboot_text"
    fi
    ;;
  *)
    echo "WARN the record of the monthly update is unreadable: $STATE"
    ;;
esac
