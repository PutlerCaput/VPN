#!/usr/bin/env bash
# Frees disk space on a VPN server by removing only regenerable caches and old logs.
# Runs on the server (DE):
#
#   bash scripts/server_disk_cleanup.sh               show what would be removed
#   bash scripts/server_disk_cleanup.sh --apply       remove it
#   bash scripts/server_disk_cleanup.sh --apply --with-deb   also the old installer
#
# Removed: the apt package cache, the systemd journal beyond 100 MB, the firmware
# update cache, atop logs older than 14 days. Nothing under /root is removed except
# the cloudflared installer, and only with --with-deb. Backups, credentials, the
# Docker volumes and the bot's data are never touched.
set -euo pipefail

APT_BIN="${APT_BIN:-apt-get}"
JOURNAL_BIN="${JOURNAL_BIN:-journalctl}"
FWUPD_DIR="${FWUPD_DIR:-/var/cache/fwupd}"
ATOP_DIR="${ATOP_DIR:-/var/log/atop}"
DEB_FILE="${DEB_FILE:-/root/cloudflared.deb}"
JOURNAL_MAX="${JOURNAL_MAX:-100M}"
ATOP_KEEP_DAYS="${ATOP_KEEP_DAYS:-14}"

APPLY=0
WITH_DEB=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --with-deb) WITH_DEB=1 ;;
    *) printf 'usage: %s [--apply] [--with-deb]\n' "$0" >&2; exit 2 ;;
  esac
done

say() { printf '%s\n' "$*"; }
size_of() { du -sk "$1" 2>/dev/null | awk '{print int($1 / 1024) " MB"}' || echo "?"; }

run() {
  if ((APPLY == 0)); then
    say "  would run: $*"
  else
    say "  run: $*"
    "$@"
  fi
}

say "apt cache:       $(size_of /var/cache/apt)"
run "$APT_BIN" clean

say "systemd journal: $("$JOURNAL_BIN" --disk-usage 2>/dev/null | sed 's/.* take up //' || echo '?')"
run "$JOURNAL_BIN" --vacuum-size="$JOURNAL_MAX"

say "firmware cache:  $(size_of "$FWUPD_DIR")"
if [[ -d "$FWUPD_DIR" ]]; then
  if ((APPLY == 0)); then
    say "  would remove: $FWUPD_DIR/*"
  else
    find "$FWUPD_DIR" -mindepth 1 -delete
    say "  removed: $FWUPD_DIR/*"
  fi
fi

say "atop logs older than ${ATOP_KEEP_DAYS} days in $ATOP_DIR:"
if [[ -d "$ATOP_DIR" ]]; then
  old="$(find "$ATOP_DIR" -type f -mtime +"$ATOP_KEEP_DAYS" | wc -l)"
  say "  files: $old"
  if ((APPLY == 1)); then
    find "$ATOP_DIR" -type f -mtime +"$ATOP_KEEP_DAYS" -delete
  fi
fi

if ((WITH_DEB == 1)); then
  if [[ -f "$DEB_FILE" ]]; then
    if ((APPLY == 0)); then
      say "would remove: $DEB_FILE"
    else
      rm -f -- "$DEB_FILE"
      say "removed: $DEB_FILE"
    fi
  else
    say "no $DEB_FILE"
  fi
fi

if ((APPLY == 0)); then
  say "DISK_CLEANUP_DRY_RUN: nothing removed. Run with --apply."
else
  say "DISK_CLEANUP_OK"
fi
