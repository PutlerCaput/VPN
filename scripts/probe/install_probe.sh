#!/usr/bin/env bash
# Installs the availability probe on a phone with a Russian SIM (Termux):
# copies the script, writes its settings, measures once and schedules it.
#
#   bash install_probe.sh --site https://app.example.com \
#       --entry 203.0.113.5:443 --entry 203.0.113.5:8443 [--period 15]
#   bash install_probe.sh --uninstall       stop the schedule, keep the log
#
# --entry is the address of a VPN entry point: host:port, or host:port:servername
# when the entry answers to a name (then the whole TLS path is measured). The
# phone needs curl (pkg install curl); the schedule needs the Termux:API app and
# pkg install termux-api. docs/PROBE_RU_MOBILE.md explains the rest.
set -euo pipefail

export PROBE_HOME="${PROBE_HOME:-$HOME/vpn-probe}"
JOB_ID="${PROBE_JOB_ID:-4712}"
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

say() { printf '%s\n' "$*"; }
stop() {
  printf 'PROBE_INSTALL_STOPPED: %s\n' "$*" >&2
  exit 1
}

site=""
entries=()
period=15
net=""
operator=""
uninstall=0
while (($#)); do
  case "$1" in
    --uninstall)
      uninstall=1
      shift
      ;;
    --site | --entry | --period | --net | --operator)
      (($# >= 2)) || stop "$1 needs a value"
      case "$1" in
        --site) site="$2" ;;
        --entry) entries+=("$2") ;;
        --period) period="$2" ;;
        --net) net="$2" ;;
        --operator) operator="$2" ;;
      esac
      shift 2
      ;;
    *) stop "unknown option: $1" ;;
  esac
done

if ((uninstall)); then
  if command -v termux-job-scheduler >/dev/null 2>&1; then
    termux-job-scheduler --cancel --job-id "$JOB_ID" >/dev/null 2>&1 || true
  fi
  rm -f "$PROBE_HOME/run.sh"
  say "PROBE_UNINSTALLED: no longer scheduled. The log and the settings stay in $PROBE_HOME."
  exit 0
fi

[[ -f "$HERE/vpn_probe.sh" ]] || stop "vpn_probe.sh must lie next to this script"
command -v curl >/dev/null 2>&1 || stop "curl is missing: pkg install curl"
[[ -n "$site" || ${#entries[@]} -gt 0 ]] || stop "give the site and/or at least one --entry"
if [[ -n "$site" ]]; then
  [[ "$site" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]] \
    || stop "--site must look like https://app.example.com (no path)"
fi
for entry in ${entries[@]+"${entries[@]}"}; do
  [[ "$entry" =~ ^[A-Za-z0-9.-]+:[0-9]{1,5}(:[A-Za-z0-9.-]+)?$ ]] \
    || stop "--entry must look like host:port or host:port:servername, not: $entry"
done
[[ "$period" =~ ^[0-9]{1,4}$ && "$period" -ge 15 && "$period" -le 1440 ]] \
  || stop "--period is in minutes, 15..1440 (Android runs periodic jobs at most every 15 minutes)"
[[ -z "$net" || "$net" == mobile || "$net" == wifi ]] || stop "--net is mobile or wifi"
[[ -z "$operator" || "$operator" =~ ^[A-Za-z0-9._-]{1,24}$ ]] || stop "--operator is one word of letters, digits . _ -"

install -d -m 0700 "$PROBE_HOME"
install -m 0700 "$HERE/vpn_probe.sh" "$PROBE_HOME/vpn_probe.sh"
{
  printf '# Written by install_probe.sh. KEY=VALUE lines; this file is never run as code.\n'
  printf 'PROBE_SITE_URL=%s\n' "$site"
  printf 'PROBE_ENTRIES=%s\n' "${entries[*]-}"
  [[ -z "$net" ]] || printf 'PROBE_NET=%s\n' "$net"
  [[ -z "$operator" ]] || printf 'PROBE_OPERATOR=%s\n' "$operator"
} >"$PROBE_HOME/probe.conf.new"
chmod 0600 "$PROBE_HOME/probe.conf.new"
mv "$PROBE_HOME/probe.conf.new" "$PROBE_HOME/probe.conf"

# What the Android job scheduler starts: one measurement, output kept short.
shell="${PREFIX:-}/bin/bash"
[[ -x "$shell" ]] || shell=/bin/bash
cat >"$PROBE_HOME/run.sh" <<EOF
#!$shell
# Started by termux-job-scheduler: one measurement.
export PROBE_HOME="$PROBE_HOME"
if [ -f "$PROBE_HOME/run.out" ] && [ "\$(wc -c < "$PROBE_HOME/run.out")" -gt 200000 ]; then : > "$PROBE_HOME/run.out"; fi
exec "$shell" "$PROBE_HOME/vpn_probe.sh" run >>"$PROBE_HOME/run.out" 2>&1
EOF
chmod 0700 "$PROBE_HOME/run.sh"

say "== One measurement now"
bash "$PROBE_HOME/vpn_probe.sh" run \
  || stop "the first measurement failed: look at the message above and at $PROBE_HOME/probe.conf"

if command -v termux-job-scheduler >/dev/null 2>&1; then
  termux-job-scheduler --job-id "$JOB_ID" --script "$PROBE_HOME/run.sh" \
    --period-ms "$((period * 60000))" --network any --battery-not-low false --persisted true >/dev/null \
    || stop "the schedule was not set (is the Termux:API app installed and allowed to run in the background?)"
  say ""
  say "PROBE_INSTALLED: measuring every $period min. Look:  bash $PROBE_HOME/vpn_probe.sh summary"
else
  say ""
  say "PROBE_INSTALLED_NOT_SCHEDULED: the script is in place but nothing starts it."
  say "Install the Termux:API app (F-Droid) and  pkg install termux-api , then run this again."
  say "Until then:  bash $PROBE_HOME/vpn_probe.sh run"
fi
