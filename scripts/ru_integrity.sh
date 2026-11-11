#!/usr/bin/env bash
# Integrity watch for the RU server: has anything on it been changed behind your back?
#   bash ru_integrity.sh            report: what is installed and the last result
#   bash ru_integrity.sh --apply    install AIDE, rkhunter and auditd (no mail server),
#                                   take today's state as the baseline, check weekly
#   bash ru_integrity.sh --check    run the weekly check now; a short summary
#   bash ru_integrity.sh --accept   after a change YOU made (an update, a new setting):
#                                   take today's state as the new baseline
#   bash ru_integrity.sh --status   one line: OK or WARN
#
# Three independent looks, because each one can be fooled on its own:
#   dpkg -V    system files against the checksums of the packages they came from
#   AIDE       system folders, /etc, root's ssh keys, the gateway's code against
#              the baseline taken at --apply or --accept
#   rkhunter   known rootkit traces
# auditd records who changed the sensitive files, for looking back afterwards.
#
# An antivirus is not installed on purpose: this server receives no files or mail,
# and the scanners that exist need more memory than it has (docs/RU_HARDENING.md).
# Nothing here blocks or deletes anything: it only reports.
set -euo pipefail

PREFIX="${PREFIX:-}"                      # tests keep state under a temporary root
CONF_DIR="$PREFIX/etc/vpn-integrity"
STATE="$PREFIX/var/lib/vpn-integrity"
LOGS="$PREFIX/var/log/vpn-integrity"
AIDE_CONF="$CONF_DIR/aide.conf"
SUMMARY="$LOGS/last-check.txt"
UNIT_DIR="$PREFIX/etc/systemd/system"
UNIT="vpn-integrity-check"
INSTALLED="$PREFIX/usr/local/sbin/ru_integrity.sh"
AUDIT_RULES="$PREFIX/etc/audit/rules.d/vpn-integrity.rules"
RKHUNTER_LOCAL="$PREFIX/etc/rkhunter.conf.local"
STALE_AFTER_DAYS=9
# What must change only when you update or reconfigure the server.
WATCH="${WATCH:-/boot /usr/bin /usr/sbin /usr/lib/systemd/system /usr/local /etc /root/.ssh /opt/vpn-db/app}"
# These change by themselves and would raise a false alarm every week.
SKIP="${SKIP:-/etc/mtab /etc/resolv.conf /etc/adjtime /etc/ld.so.cache /etc/iptables/.firewall-pending /etc/vpn-integrity}"
# Files whose every change auditd writes down.
AUDITED="/etc/passwd /etc/shadow /etc/sudoers /etc/sudoers.d /etc/ssh/sshd_config /etc/ssh/sshd_config.d /root/.ssh /etc/iptables /etc/wireguard /etc/vpn-db /etc/systemd/system"

SELF="$(readlink -f "$0")"
say() { printf '%s\n' "$*"; }
stop() {
  printf 'RU_INTEGRITY_STOPPED: %s\n' "$*" >&2
  exit 1
}
as_root() { [[ "$(id -u)" == 0 ]] || stop "run as root"; }
have() { command -v "$1" >/dev/null 2>&1; }
field() { sed -n "s/^$1=//p" "$SUMMARY" 2>/dev/null | tail -1; }

write_aide_conf() {
  local path
  {
    printf '# Written by scripts/ru_integrity.sh. A focused list instead of the distribution'"'"'s\n'
    printf '# default: fewer files, so a change in the report means something.\n'
    printf 'database_in=file:%s/aide.db\n' "$STATE"
    printf 'database_out=file:%s/aide.db.new\n' "$STATE"
    printf 'database_new=file:%s/aide.db.new\n' "$STATE"
    printf 'gzip_dbout=yes\nreport_summarize_changes=yes\nlog_level=warning\n'
    printf 'report_level=changed_attributes\n'
    printf 'Watched = p+i+n+u+g+s+m+c+sha256\n'
    for path in $SKIP; do printf '!%s$\n' "$path"; done
    # "if", not "&&": a missing last folder must not end the script under set -e.
    for path in $WATCH; do
      if [[ -e "$path" ]]; then printf '%s Watched\n' "$path"; fi
    done
  } >"$AIDE_CONF"
}

rkhunter_warnings() {
  # Sorted, so two runs can be compared line by line.
  rkhunter --check --sk --rwo --nocolors 2>&1 | grep '^Warning:' | sort -u || true
}

take_baseline() {
  # The file properties rkhunter compares against, then AIDE's database.
  if have rkhunter; then
    rkhunter --propupd --nocolors >/dev/null 2>&1 || say "WARN: rkhunter could not record its baseline"
    # What rkhunter says about this server as it is today is "known": a stock
    # system always has a few. Only warnings that appear later are news.
    rkhunter_warnings >"$STATE/rkhunter-known.txt"
  fi
  rm -f "$STATE/aide.db.new"
  aide --config="$AIDE_CONF" --init >"$LOGS/aide-init.txt" 2>&1 \
    || stop "AIDE could not build the baseline (see $LOGS/aide-init.txt)"
  [[ -s "$STATE/aide.db.new" ]] || stop "AIDE wrote no baseline"
  mv "$STATE/aide.db.new" "$STATE/aide.db"
  chmod 0600 "$STATE/aide.db"
}

apply() {
  as_root
  have systemctl || stop "systemd is required"
  local missing="" tool value
  for tool in aide rkhunter auditctl; do have "$tool" || missing=1; done
  if [[ -n "$missing" ]]; then
    say "installing aide, rkhunter and auditd (without a mail server)..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
      aide aide-common rkhunter auditd >/dev/null \
      || stop "apt could not install the packages; nothing else was changed"
  fi
  have aide || stop "aide is still missing after the installation"
  install -d -m 0700 "$CONF_DIR" "$STATE" "$LOGS"
  install -d -m 0755 "$(dirname "$INSTALLED")" "$UNIT_DIR"
  install -m 0700 "$SELF" "$INSTALLED"
  # The distribution's own daily AIDE run uses its broad default list: one check
  # with one list is enough, and it would mail nobody anyway.
  systemctl disable --now dailyaidecheck.timer >/dev/null 2>&1 || true
  write_aide_conf
  aide --config="$AIDE_CONF" --config-check >/dev/null 2>&1 || stop "the AIDE settings were rejected by aide itself"

  if have rkhunter; then
    # Root logs in by key here; tell rkhunter the value sshd really uses, or it
    # warns about it forever. Package checksums come from dpkg.
    value="$(sshd -T 2>/dev/null | awk '$1 == "permitrootlogin" { print $2 }' || true)"
    {
      printf '# Written by scripts/ru_integrity.sh.\nPKGMGR=DPKG\n'
      if [[ -n "$value" ]]; then printf 'ALLOW_SSH_ROOT_USER=%s\n' "$value"; fi
    } >"$RKHUNTER_LOCAL"
  fi

  if have augenrules; then
    install -d -m 0750 "$(dirname "$AUDIT_RULES")"
    {
      printf '# Written by scripts/ru_integrity.sh: who changed the sensitive files.\n'
      for tool in $AUDITED; do
        if [[ -e "$tool" ]]; then printf -- '-w %s -p wa -k vpn-integrity\n' "$tool"; fi
      done
    } >"$AUDIT_RULES"
    chmod 0640 "$AUDIT_RULES"
    systemctl enable --now auditd >/dev/null 2>&1 || true
    augenrules --load >/dev/null 2>&1 || say "WARN: the audit rules were written but not loaded (they load at the next boot)"
  fi

  say "taking the baseline (a minute or two)..."
  take_baseline
  cat >"$UNIT_DIR/$UNIT.service" <<EOF
[Unit]
Description=Weekly integrity check of this server (scripts/ru_integrity.sh)

[Service]
Type=oneshot
ExecStart=/bin/bash $INSTALLED --check
Nice=19
IOSchedulingClass=idle
TimeoutStartSec=1h
EOF
  cat >"$UNIT_DIR/$UNIT.timer" <<EOF
[Unit]
Description=Weekly integrity check of this server

[Timer]
OnCalendar=Sun *-*-* 01:30:00 UTC
RandomizedDelaySec=30min
Persistent=true

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload || stop "systemctl daemon-reload failed"
  systemctl enable --now "$UNIT.timer" >/dev/null 2>&1 || stop "could not switch the weekly check on"
  check quiet
  if [[ -s "$STATE/rkhunter-known.txt" ]]; then
    say "rkhunter's $(grep -c . "$STATE/rkhunter-known.txt") warnings about the server as it is today are taken as known."
    say "Look at them once: $STATE/rkhunter-known.txt"
  fi
  say "RU_INTEGRITY_ON: baseline taken, the check runs every Sunday night."
  say "See the result any time:  bash $INSTALLED --status"
  say "After an update or a change of your own:  bash $INSTALLED --accept"
}

check() {
  as_root
  [[ -s "$STATE/aide.db" ]] || stop "there is no baseline yet: run --apply first"
  install -d -m 0700 "$LOGS"
  local dpkg_modified=0 added=0 removed=0 changed=0 warnings=0 updated=no rc=0
  # c = a configuration file: changing those is normal, AIDE watches them instead.
  if have dpkg; then
    dpkg -V 2>/dev/null | awk '$2 != "c"' >"$LOGS/dpkg.txt" || true
    dpkg_modified="$(grep -c . "$LOGS/dpkg.txt" || true)"
  fi
  aide --config="$AIDE_CONF" --check >"$LOGS/aide.txt" 2>&1 || rc=$?
  # 0 = nothing changed; 1..7 = added, removed and changed, as a sum; more = an error.
  ((rc <= 7)) || stop "AIDE could not run its check (see $LOGS/aide.txt)"
  added="$(awk -F'\t' '/Added entries:/ { print $NF; exit }' "$LOGS/aide.txt")"
  removed="$(awk -F'\t' '/Removed entries:/ { print $NF; exit }' "$LOGS/aide.txt")"
  changed="$(awk -F'\t' '/Changed entries:/ { print $NF; exit }' "$LOGS/aide.txt")"
  if have rkhunter; then
    rkhunter_warnings >"$LOGS/rkhunter-all.txt"
    touch "$STATE/rkhunter-known.txt"
    comm -13 "$STATE/rkhunter-known.txt" "$LOGS/rkhunter-all.txt" >"$LOGS/rkhunter.txt"
    warnings="$(grep -c . "$LOGS/rkhunter.txt" || true)"
  fi
  # Packages installed or updated since the baseline explain changed system files.
  if [[ -n "$(find "$PREFIX/var/lib/dpkg/status" -newer "$STATE/aide.db" 2>/dev/null)" ]]; then
    updated=yes
  fi
  {
    printf 'checked_at=%s\n' "$(date +%s)"
    printf 'dpkg_modified=%s\n' "${dpkg_modified:-0}"
    printf 'aide_added=%s\naide_removed=%s\naide_changed=%s\n' "${added:-0}" "${removed:-0}" "${changed:-0}"
    printf 'rkhunter_new_warnings=%s\n' "${warnings:-0}"
    printf 'packages_updated_since_baseline=%s\n' "$updated"
  } >"$SUMMARY.new"
  mv "$SUMMARY.new" "$SUMMARY"
  [[ "${1:-}" == quiet ]] || status
}

accept() {
  as_root
  [[ -f "$AIDE_CONF" ]] || stop "not set up yet: run --apply first"
  write_aide_conf
  take_baseline
  check quiet
  say "RU_INTEGRITY_ACCEPTED: today's state is the new baseline."
}

status() {
  if [[ ! -s "$SUMMARY" ]]; then
    say "WARN integrity: never checked (bash ru_integrity.sh --apply)"
    return
  fi
  local age modified total warnings problems=""
  age=$((($(date +%s) - $(field checked_at)) / 86400))
  modified="$(field dpkg_modified)"
  total=$(($(field aide_added) + $(field aide_removed) + $(field aide_changed)))
  warnings="$(field rkhunter_new_warnings)"
  ((age <= STALE_AFTER_DAYS)) || problems+="last check $age days ago; "
  ((modified == 0)) || problems+="$modified system files differ from their packages (look at $LOGS/dpkg.txt now); "
  if ((total > 0)); then
    problems+="$total files changed since the baseline"
    if [[ "$(field packages_updated_since_baseline)" == yes ]]; then
      problems+=" (packages were updated since then)"
    fi
    problems+=": if that was you or an update, run --accept; else look at $LOGS/aide.txt; "
  fi
  ((warnings == 0)) || problems+="rkhunter has $warnings new warnings ($LOGS/rkhunter.txt); "
  if [[ -n "$problems" ]]; then
    say "WARN integrity: ${problems%; }"
  else
    say "OK integrity: checked $age days ago, nothing changed"
  fi
}

report() {
  say "== Integrity watch on this server"
  local tool
  for tool in aide rkhunter auditctl; do
    say "  $tool: $(have "$tool" && echo installed || echo MISSING)"
  done
  if [[ -s "$STATE/aide.db" ]]; then
    say "  baseline: taken $(date -u -r "$STATE/aide.db" '+%Y-%m-%d %H:%M UTC')"
  else
    say "  baseline: none yet (--apply takes it)"
  fi
  say "  weekly check: $(systemctl is-enabled "$UNIT.timer" 2>/dev/null || echo off)"
  say "  $(status)"
  say "RU_INTEGRITY_REPORT_DONE"
}

case "${1:-}" in
  "") report ;;
  --apply) apply ;;
  --check) check ;;
  --accept) accept ;;
  --status) status ;;
  *) stop "usage: ru_integrity.sh [--apply | --check | --accept | --status]" ;;
esac
