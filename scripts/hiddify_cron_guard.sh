#!/usr/bin/env bash
# Keeps Hiddify's nightly auto-update switched off on the DE server.
#
#   bash scripts/hiddify_cron_guard.sh --install   # copy itself, path + timer units, one pass now
#   bash scripts/hiddify_cron_guard.sh --run       # one pass: comment out an active auto-update line
#   bash scripts/hiddify_cron_guard.sh --status    # one doctor line: "OK <text>" or "WARN <text>"
#   bash scripts/hiddify_cron_guard.sh --remove    # take the units away (the cron file stays as it is)
#
# Why: the nightly update (0 3 * * * .../update.sh) re-applies Hiddify's
# defaults over the port overrides this project depends on, so it stays
# commented out and Hiddify is updated by hand. But Hiddify writes the line back
# by itself: /etc/cron.d/hiddify_reinstall_on_reboot runs scripts/install.sh at
# every boot, and its scripts/common/run.sh rewrites
# /etc/cron.d/hiddify_auto_update with an active line (so does a manual
# install.sh or apply_configs). The guard reacts to every change of that file
# (a systemd path unit), and a timer looks again 5 minutes after boot and every
# 15 minutes, in case a change slipped past while the path unit was not loaded.
# Before a change the file is copied to $BACKUP_DIR; nothing else is touched.
set -euo pipefail

fail() { printf 'HIDDIFY_CRON_GUARD_FAILED: %s\n' "$*" >&2; exit 1; }
usage() { fail "usage: hiddify_cron_guard.sh --install | --run | --status | --remove"; }
[[ $# -eq 1 ]] || usage

# An ETC_DIR given by the caller is a sandbox (tests), not the server.
SANDBOX=0
if [[ -n "${ETC_DIR:-}" ]]; then
  SANDBOX=1
else
  ETC_DIR=/etc
fi
CRON_FILE="${CRON_FILE:-$ETC_DIR/cron.d/hiddify_auto_update}"
UNIT_DIR="${UNIT_DIR:-$ETC_DIR/systemd/system}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/hiddify-cron-guard}"
INSTALL_PATH="${INSTALL_PATH:-/usr/local/sbin/hiddify-cron-guard}"
UNIT=hiddify-cron-guard

need_root() { ((SANDBOX)) || [[ $EUID -eq 0 ]] || fail "run as root"; }

# Lines that would run: not empty, not a comment.
active_lines() { [[ -f "$CRON_FILE" ]] && grep -nEv '^[[:space:]]*(#|$)' "$CRON_FILE" || true; }

run_once() {
  need_root
  [[ -f "$CRON_FILE" ]] || { printf 'HIDDIFY_CRON_GUARD_OK: %s does not exist\n' "$CRON_FILE"; return 0; }
  if [[ -z "$(active_lines)" ]]; then
    printf 'HIDDIFY_CRON_GUARD_OK: the auto-update is already off\n'
    return 0
  fi
  local stamp tmp
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$BACKUP_DIR"
  cp -p "$CRON_FILE" "$BACKUP_DIR/hiddify_auto_update.$stamp"
  tmp="$(mktemp "$CRON_FILE.guard.XXXXXX")"
  # Every active line is kept, commented, so the owner sees what Hiddify wanted.
  sed -E "/^[[:space:]]*(#|\$)/! s/^/# disabled $stamp by hiddify-cron-guard (port overrides, update manually): /" \
    "$CRON_FILE" >"$tmp"
  chmod 644 "$tmp"
  mv -f "$tmp" "$CRON_FILE"
  # cron reads /etc/cron.d again when the file's time changes; a reload is a courtesy.
  if ((!SANDBOX)) && command -v systemctl >/dev/null 2>&1; then
    systemctl reload cron >/dev/null 2>&1 || true
  fi
  command -v logger >/dev/null 2>&1 && logger -t hiddify-cron-guard "auto-update line commented out again (backup $BACKUP_DIR/hiddify_auto_update.$stamp)" || true
  printf 'HIDDIFY_CRON_GUARD_FIXED: the auto-update line was active and is commented out again (backup %s)\n' \
    "$BACKUP_DIR/hiddify_auto_update.$stamp"
}

status() {
  if [[ -n "$(active_lines)" ]]; then
    echo "WARN Hiddify nightly auto-update is ACTIVE in $CRON_FILE: bash scripts/hiddify_cron_guard.sh --run"
    return 0
  fi
  if [[ ! -f "$UNIT_DIR/$UNIT.path" || ! -f "$UNIT_DIR/$UNIT.timer" ]]; then
    echo "WARN Hiddify auto-update is off, but nothing keeps it off after a reboot: bash scripts/hiddify_cron_guard.sh --install"
    return 0
  fi
  if ((!SANDBOX)) && command -v systemctl >/dev/null 2>&1; then
    if ! systemctl is-active --quiet "$UNIT.path" || ! systemctl is-active --quiet "$UNIT.timer"; then
      echo "WARN the Hiddify auto-update guard is installed but not active: systemctl enable --now $UNIT.path $UNIT.timer"
      return 0
    fi
  fi
  echo "OK Hiddify nightly auto-update is off and guarded (also after a reboot)"
}

install_units() {
  need_root
  local self
  self="$(readlink -f "$0")"
  mkdir -p "$(dirname "$INSTALL_PATH")" "$UNIT_DIR"
  # A copy outside the checkout: the guard does not depend on what is checked out.
  if [[ "$self" != "$(readlink -f "$INSTALL_PATH" 2>/dev/null || true)" ]]; then
    install -m 0755 "$self" "$INSTALL_PATH"
  fi
  cat >"$UNIT_DIR/$UNIT.service" <<UNIT_EOF
[Unit]
Description=Keep Hiddify's nightly auto-update commented out (port overrides)

[Service]
Type=oneshot
ExecStart=/bin/bash $INSTALL_PATH --run
UNIT_EOF
  cat >"$UNIT_DIR/$UNIT.path" <<UNIT_EOF
[Unit]
Description=Watch Hiddify's auto-update cron file

[Path]
PathChanged=$CRON_FILE
Unit=$UNIT.service

[Install]
WantedBy=multi-user.target
UNIT_EOF
  cat >"$UNIT_DIR/$UNIT.timer" <<UNIT_EOF
[Unit]
Description=Look at Hiddify's auto-update cron file after boot and every 15 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min
Unit=$UNIT.service

[Install]
WantedBy=timers.target
UNIT_EOF
  if ((!SANDBOX)); then
    systemctl daemon-reload
    systemctl enable --now "$UNIT.path" "$UNIT.timer" >/dev/null
  fi
  run_once
  printf 'HIDDIFY_CRON_GUARD_INSTALLED: %s.path watches %s; %s.timer looks again every 15 minutes\n' \
    "$UNIT" "$CRON_FILE" "$UNIT"
}

remove_units() {
  need_root
  if ((!SANDBOX)); then
    systemctl disable --now "$UNIT.path" "$UNIT.timer" >/dev/null 2>&1 || true
  fi
  rm -f "$UNIT_DIR/$UNIT.service" "$UNIT_DIR/$UNIT.path" "$UNIT_DIR/$UNIT.timer"
  ((SANDBOX)) || systemctl daemon-reload
  printf 'HIDDIFY_CRON_GUARD_REMOVED: units removed; %s left as it is\n' "$CRON_FILE"
}

case "$1" in
  --run) run_once ;;
  --status) status ;;
  --install) install_units ;;
  --remove) remove_units ;;
  *) usage ;;
esac
