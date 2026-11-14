#!/usr/bin/env bash
# Installs the availability probe on a phone with a Russian SIM (Termux):
# copies the script, writes its settings, measures once and starts it.
#
#   bash install_probe.sh --site https://app.example.com \
#       --entry 203.0.113.5:443 --entry 203.0.113.5:8443 [--period 15] [--loop]
#   bash install_probe.sh --uninstall       stop it, keep the log
#
# --entry is the address of a VPN entry point: host:port, or host:port:servername
# when the entry answers to a name. How it is started:
#   default  the Android job scheduler (termux-job-scheduler). It needs the
#            Termux:API app, which the Google Play build of Termux does not have
#   --loop   a loop in the background (vpn_probe.sh loop): works everywhere, but
#            Termux has to stay alive and the loop is started again after a reboot
# The schedule is checked, not trusted: termux-job-scheduler can exit with 0 and
# do nothing. docs/PROBE_RU_MOBILE.md explains the rest.
set -euo pipefail

export PROBE_HOME="${PROBE_HOME:-$HOME/vpn-probe}"
JOB_ID="${PROBE_JOB_ID:-4712}"
export PROBE_JOB_ID="$JOB_ID"
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"

say() { printf '%s\n' "$*"; }
stop() {
  printf 'PROBE_INSTALL_STOPPED: %s\n' "$*" >&2
  exit 1
}
# A Termux:API command, never for long: without the app some versions wait forever.
bounded() {
  if command -v timeout >/dev/null 2>&1; then
    timeout "${PROBE_API_TIMEOUT:-20}" "$@" 2>&1 || true
  else
    "$@" 2>&1 || true
  fi
}

site=""
entries=()
period=15
net=""
operator=""
uninstall=0
use_loop=0
while (($#)); do
  case "$1" in
    --uninstall)
      uninstall=1
      shift
      ;;
    --loop)
      use_loop=1
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
  if [[ -f "$PROBE_HOME/vpn_probe.sh" ]]; then
    bash "$PROBE_HOME/vpn_probe.sh" loop-stop >/dev/null 2>&1 || true
  fi
  if command -v termux-job-scheduler >/dev/null 2>&1; then
    bounded termux-job-scheduler --cancel --job-id "$JOB_ID" >/dev/null
  fi
  rm -f "$PROBE_HOME/run.sh"
  say "PROBE_UNINSTALLED: no longer running. The log and the settings stay in $PROBE_HOME."
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
[[ "$period" =~ ^[1-9][0-9]{0,3}$ && "$period" -le 1440 ]] \
  || stop "--period is in minutes, 1..1440"
if ((!use_loop)); then
  ((period >= 15)) || stop "--period under 15 minutes needs --loop (Android runs scheduled jobs at most every 15 minutes)"
fi
[[ -z "$net" || "$net" == mobile || "$net" == wifi ]] || stop "--net is mobile or wifi"
[[ -z "$operator" || "$operator" =~ ^[A-Za-z0-9._-]{1,24}$ ]] || stop "--operator is one word of letters, digits . _ -"

install -d -m 0700 "$PROBE_HOME"
install -m 0700 "$HERE/vpn_probe.sh" "$PROBE_HOME/vpn_probe.sh"
{
  printf '# Written by install_probe.sh. KEY=VALUE lines; this file is never run as code.\n'
  printf 'PROBE_SITE_URL=%s\n' "$site"
  printf 'PROBE_ENTRIES=%s\n' "${entries[*]-}"
  printf 'PROBE_PERIOD=%s\n' "$period"
  [[ -z "$net" ]] || printf 'PROBE_NET=%s\n' "$net"
  [[ -z "$operator" ]] || printf 'PROBE_OPERATOR=%s\n' "$operator"
} >"$PROBE_HOME/probe.conf.new"
chmod 0600 "$PROBE_HOME/probe.conf.new"
mv "$PROBE_HOME/probe.conf.new" "$PROBE_HOME/probe.conf"

# What the Android job scheduler starts: one measurement, output kept short.
shell="${PREFIX:-}/bin/bash"
[[ -x "$shell" ]] || shell=/bin/bash
cat >"$PROBE_HOME/run.sh" <<RUNNER
#!$shell
# Started by termux-job-scheduler: one measurement.
export PROBE_HOME="$PROBE_HOME"
if [ -f "$PROBE_HOME/run.out" ] && [ "\$(wc -c < "$PROBE_HOME/run.out")" -gt 200000 ]; then : > "$PROBE_HOME/run.out"; fi
exec "$shell" "$PROBE_HOME/vpn_probe.sh" run >>"$PROBE_HOME/run.out" 2>&1
RUNNER
chmod 0700 "$PROBE_HOME/run.sh"

# An earlier loop would keep measuring with the old settings.
bash "$PROBE_HOME/vpn_probe.sh" loop-stop >/dev/null 2>&1 || true

say "== One measurement now"
bash "$PROBE_HOME/vpn_probe.sh" run \
  || stop "the first measurement failed: look at the message above and at $PROBE_HOME/probe.conf"
say ""
say "== What it saw"
bash "$PROBE_HOME/vpn_probe.sh" last || true
say ""

if ((use_loop)); then
  # One runner only: a job left in the Android schedule would measure beside the loop.
  if command -v termux-job-scheduler >/dev/null 2>&1; then
    if grep -q "$JOB_ID" <<<"$(bounded termux-job-scheduler --pending)"; then
      bounded termux-job-scheduler --cancel --job-id "$JOB_ID" >/dev/null
      say "The Android job $JOB_ID was cancelled: from now on the loop measures, and only it."
    fi
  fi
  nohup bash "$PROBE_HOME/vpn_probe.sh" loop "$period" >>"$PROBE_HOME/run.out" 2>&1 &
  disown 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    [[ -s "$PROBE_HOME/loop.pid" ]] && break
    sleep 0.3
  done
  if [[ -s "$PROBE_HOME/loop.pid" ]]; then
    say "PROBE_INSTALLED: measuring every $period min in a loop (pid $(cat "$PROBE_HOME/loop.pid"))."
    say "Termux has to stay alive (leave it open, battery optimisation off). After a reboot start it again:"
    say "  nohup bash $PROBE_HOME/vpn_probe.sh loop >/dev/null 2>&1 &"
    say "Stop:  bash $PROBE_HOME/vpn_probe.sh loop-stop"
  else
    stop "the loop did not start: look at $PROBE_HOME/run.out"
  fi
elif command -v termux-job-scheduler >/dev/null 2>&1; then
  asked="$(bounded termux-job-scheduler --job-id "$JOB_ID" --script "$PROBE_HOME/run.sh" \
    --period-ms "$((period * 60000))" --network any --battery-not-low false --persisted true)"
  pending="$(bounded termux-job-scheduler --pending)"
  if grep -q "$JOB_ID" <<<"$pending"; then
    say "PROBE_INSTALLED: measuring every $period min (job $JOB_ID is in the schedule). Look:  bash $PROBE_HOME/vpn_probe.sh summary"
    say "A job in the list is not yet a run: in $((period + 5)) minutes  bash $PROBE_HOME/vpn_probe.sh status  must show a newer run."
    say "If it does not, run this again with  --loop  added."
  elif grep -qiE 'google play|not installed|not available' <<<"$asked
$pending"; then
    say "PROBE_INSTALLED_NOT_SCHEDULED: the Termux:API app is not available on this phone (the Google Play build of Termux has none)."
    say "Nothing starts the measurements. Use the loop instead: run this again with  --loop  added."
  else
    say "PROBE_INSTALLED_UNVERIFIED: the schedule was asked for, but termux-job-scheduler --pending does not list job $JOB_ID."
    say "Look in 20 minutes:  bash $PROBE_HOME/vpn_probe.sh status . If no new run appears, run this again with  --loop  added."
  fi
else
  say "PROBE_INSTALLED_NOT_SCHEDULED: termux-job-scheduler is missing, so nothing starts the measurements."
  say "Either  pkg install termux-api  and the Termux:API app (F-Droid), or run this again with  --loop  added."
fi
