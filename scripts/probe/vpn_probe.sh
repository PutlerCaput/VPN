#!/usr/bin/env bash
# Availability probe for a phone on a Russian mobile network (Termux on Android).
#
# It measures, from where the customers are, whether OUR service answers:
#   - the site (GET <site>/healthz),
#   - the VPN entry points (TCP connect, and TLS when a server name is given),
#   - Telegram (api.telegram.org, t.me): the sales and support channel,
#   - two control hosts (one Russian, one foreign): if a control is down, the
#     phone's own internet is down and that run says nothing about us.
# It touches nothing but these hosts, hides nothing and bypasses nothing: it only
# says what is reachable and how fast. docs/PROBE_RU_MOBILE.md explains it.
#
#   bash vpn_probe.sh run          one measurement: log it, alert on a change
#   bash vpn_probe.sh summary [H]  per network and target over the last H hours (24)
#   bash vpn_probe.sh last         the last run, target by target
#   bash vpn_probe.sh status       one line: OK or WARN (for a quick look)
#   bash vpn_probe.sh config       the settings in force
#
# Settings: $PROBE_HOME/probe.conf (KEY=VALUE lines, never run as code).
set -euo pipefail

PROBE_HOME="${PROBE_HOME:-$HOME/vpn-probe}"
CONF="$PROBE_HOME/probe.conf"
LOG="$PROBE_HOME/probe.log"
STATE="$PROBE_HOME/state"
ALERTS="$PROBE_HOME/alerts.log"
MAX_LOG_LINES="${PROBE_MAX_LOG_LINES:-40000}"
NOW="${PROBE_NOW:-$(date +%s)}"

PROBE_SITE_URL=""
PROBE_ENTRIES=""
PROBE_CONTROLS="https://ya.ru/ http://connectivitycheck.gstatic.com/generate_204"
PROBE_TELEGRAM="https://api.telegram.org/ https://t.me/"
PROBE_NET=""
PROBE_OPERATOR=""
PROBE_TIMEOUT=10
PROBE_FAIL_STREAK=3

say() { printf '%s\n' "$*"; }
stop() {
  printf 'PROBE_STOPPED: %s\n' "$*" >&2
  exit 1
}

# KEY=VALUE lines of a known key. The file is read, never sourced or evaluated.
load_config() {
  [[ -f "$CONF" ]] || return 0
  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
    [[ "$line" =~ ^([A-Z_]+)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"
    value="${BASH_REMATCH[2]}"
    value="${value%\"}"
    value="${value#\"}"
    case "$key" in
      PROBE_SITE_URL | PROBE_ENTRIES | PROBE_CONTROLS | PROBE_TELEGRAM | PROBE_NET | PROBE_OPERATOR | PROBE_TIMEOUT | PROBE_FAIL_STREAK)
        printf -v "$key" '%s' "$value"
        ;;
    esac
  done <"$CONF"
}

URL_RE='^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[^[:space:]]*)?$'
ENTRY_RE='^[A-Za-z0-9.-]+:[0-9]{1,5}(:[A-Za-z0-9.-]+)?$'

validate_config() {
  local item
  [[ "$PROBE_TIMEOUT" =~ ^[0-9]{1,2}$ && "$PROBE_TIMEOUT" -ge 1 && "$PROBE_TIMEOUT" -le 60 ]] \
    || stop "PROBE_TIMEOUT must be 1..60 seconds"
  [[ "$PROBE_FAIL_STREAK" =~ ^[0-9]{1,2}$ && "$PROBE_FAIL_STREAK" -ge 1 && "$PROBE_FAIL_STREAK" -le 20 ]] \
    || stop "PROBE_FAIL_STREAK must be 1..20 runs"
  if [[ -n "$PROBE_SITE_URL" ]]; then
    [[ "$PROBE_SITE_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]] \
      || stop "PROBE_SITE_URL must look like https://app.example.com (no path)"
  fi
  for item in $PROBE_ENTRIES; do
    [[ "$item" =~ $ENTRY_RE ]] || stop "an entry must look like host:port or host:port:servername"
    if [[ "$item" =~ ^[^:]+:[0-9]+:[^:]+$ && ! "$item" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}: ]]; then
      stop "an entry with a server name needs the server's IP address first: 203.0.113.5:443:name"
    fi
  done
  for item in $PROBE_CONTROLS $PROBE_TELEGRAM; do
    [[ "$item" =~ $URL_RE ]] || stop "not a plain URL in the settings: $item"
  done
  [[ -n "$PROBE_SITE_URL$PROBE_ENTRIES" ]] \
    || stop "nothing of ours to measure: set PROBE_SITE_URL and PROBE_ENTRIES in $CONF"
}

# What kind of network the phone is on right now, and whose: "<net> <operator>".
network_facts() {
  local net="$PROBE_NET" operator="$PROBE_OPERATOR" info
  if [[ -z "$net" ]]; then
    net=unknown
    if command -v termux-wifi-connectioninfo >/dev/null 2>&1; then
      info="$(termux-wifi-connectioninfo 2>/dev/null || true)"
      if [[ -n "$info" ]]; then
        if grep -q '"supplicant_state": *"COMPLETED"' <<<"$info"; then net=wifi; else net=mobile; fi
      fi
    fi
  fi
  if [[ -z "$operator" ]]; then
    operator=unknown
    if command -v termux-telephony-deviceinfo >/dev/null 2>&1; then
      operator="$(termux-telephony-deviceinfo 2>/dev/null \
        | sed -n 's/.*"network_operator_name": *"\([^"]*\)".*/\1/p' | head -1 || true)"
      operator="${operator:-unknown}"
    fi
  fi
  # One word per field in the log: no tabs, no odd characters.
  operator="$(printf '%s' "$operator" | tr -c 'A-Za-z0-9._-' '_' | cut -c1-24)"
  printf '%s %s\n' "$net" "${operator:-unknown}"
}

# Seconds to milliseconds. A real connection is never 0: curl prints exactly 0
# when nothing connected, and a local one can take less than a millisecond.
ms() { awk -v s="${1:-0}" 'BEGIN { v = s * 1000; printf "%d", (v > 0 && v < 1) ? 1 : v }'; }

reason_of() {
  case "$1" in
    0) printf ok ;;
    6) printf dns ;;
    7) printf refused ;;
    28) printf timeout ;;
    35 | 51 | 58 | 59 | 60 | 77 | 83 | 90 | 91) printf tls ;;
    52 | 56) printf reset ;;
    *) printf error ;;
  esac
}

# measure <curl arguments>: prints "<exit> <http> <tcp s> <tls s> <total s>".
measure() {
  local out rc=0
  out="$(curl -sS -o /dev/null --proto '=http,https' --max-time "$PROBE_TIMEOUT" --connect-timeout "$PROBE_TIMEOUT" \
    -w '%{http_code} %{time_connect} %{time_appconnect} %{time_total}' "$@" 2>/dev/null)" || rc=$?
  printf '%s %s\n' "$rc" "${out:-000 0 0 0}"
}

ROWS=()
# add_row <target> <kind> <status> <reason> <code> <tcp ms> <tls ms> <total ms>
add_row() {
  ROWS+=("$(printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s' "$@")")
}

# One target of the given kind; mode is head or get. Status "ok" means an answer
# arrived (for ours, a 200).
check_url() {
  local target="$1" kind="$2" url="$3" mode="$4" rc code tcp tls total status reason
  local -a mode_args=()
  [[ "$mode" == head ]] && mode_args=(-I)
  read -r rc code tcp tls total < <(measure "${mode_args[@]}" "$url")
  status=fail
  reason="$(reason_of "$rc")"
  if [[ "$rc" == 0 && "$code" != 000 ]]; then
    status=ok
  fi
  if [[ "$kind" == ours && "$status" == ok && "$code" != 200 ]]; then
    status=fail
    reason="http_$code"
  fi
  add_row "$target" "$kind" "$status" "$reason" "$code" "$(ms "$tcp")" "$(ms "$tls")" "$(ms "$total")"
}

check_entry() {
  local entry="$1" host port sni rc code tcp tls total status reason
  IFS=: read -r host port sni <<<"$entry"
  if [[ -n "$sni" ]]; then
    # The full path with the server name the entry answers to.
    read -r rc code tcp tls total < <(measure -k --resolve "$sni:$port:$host" "https://$sni:$port/")
    status=fail
    [[ "$rc" == 0 && "$code" != 000 ]] && status=ok
  else
    # No name to ask for: a connection that was accepted is the answer. The TLS
    # time is recorded when it came, for the eye only.
    read -r rc code tcp tls total < <(measure -k "https://$host:$port/")
    status=fail
    [[ "$(ms "$tcp")" -gt 0 ]] && status=ok
  fi
  reason="$(reason_of "$rc")"
  [[ "$status" == ok && "$rc" != 0 ]] && reason=tcp_only
  add_row "entry:$host:$port" ours "$status" "$reason" "$code" "$(ms "$tcp")" "$(ms "$tls")" "$(ms "$total")"
}

notify() {
  local target="$1" text="$2"
  printf '%s\t%s\n' "$NOW" "$text" >>"$ALERTS"
  if command -v termux-notification >/dev/null 2>&1; then
    termux-notification --id "vpn-probe-${target//[^A-Za-z0-9]/_}" --title "VPN probe" \
      --content "$text" >/dev/null 2>&1 || true
  fi
}

# State: "<target> <status> <failures in a row> <alerted 0|1>" per line.
update_state() {
  local valid="$1" net="$2" operator="$3" row target kind status streak alerted old
  touch "$STATE"
  for row in "${ROWS[@]}"; do
    IFS=$'\t' read -r target kind status _ <<<"$row"
    [[ "$kind" == control ]] && continue
    # A phone without internet says nothing about the targets.
    [[ "$valid" == 1 ]] || continue
    old="$(awk -v t="$target" '$1 == t { print $3 " " $4 }' "$STATE")"
    read -r streak alerted <<<"${old:-0 0}"
    if [[ "$status" == fail ]]; then
      streak=$((streak + 1))
      if ((streak >= PROBE_FAIL_STREAK && alerted == 0)); then
        alerted=1
        notify "$target" "Не отвечает: $target (запусков подряд: $streak, сеть $net/$operator)"
      fi
    else
      if ((alerted == 1)); then
        notify "$target" "Снова отвечает: $target (сеть $net/$operator)"
      fi
      streak=0
      alerted=0
    fi
    { awk -v t="$target" '$1 != t' "$STATE"; printf '%s %s %s %s\n' "$target" "$status" "$streak" "$alerted"; } >"$STATE.new"
    mv "$STATE.new" "$STATE"
  done
}

trim_log() {
  local lines
  lines="$(wc -l <"$LOG")"
  if ((lines > MAX_LOG_LINES)); then
    { head -n 1 "$LOG"; tail -n "$((MAX_LOG_LINES * 3 / 4))" "$LOG"; } >"$LOG.new"
    mv "$LOG.new" "$LOG"
  fi
}

run() {
  command -v curl >/dev/null 2>&1 || stop "curl is missing: pkg install curl"
  install -d -m 0700 "$PROBE_HOME"
  local item net operator valid=1 row target kind status
  # Controls first: they decide whether this run says anything about us.
  for item in $PROBE_CONTROLS; do
    check_url "control:$(url_host "$item")" control "$item" head
  done
  for row in "${ROWS[@]}"; do
    IFS=$'\t' read -r _ kind status _ <<<"$row"
    [[ "$kind" == control && "$status" == fail ]] && valid=0
  done
  [[ -z "$PROBE_SITE_URL" ]] || check_url site ours "$PROBE_SITE_URL/healthz" get
  for item in $PROBE_ENTRIES; do
    check_entry "$item"
  done
  for item in $PROBE_TELEGRAM; do
    check_url "telegram:$(url_host "$item")" telegram "$item" head
  done
  read -r net operator < <(network_facts)
  if [[ ! -s "$LOG" ]]; then
    printf 'ts\tnet\toperator\ttarget\tkind\tstatus\treason\tcode\ttcp_ms\ttls_ms\ttotal_ms\tvalid\n' >"$LOG"
  fi
  for row in "${ROWS[@]}"; do
    printf '%s\t%s\t%s\t%s\t%s\n' "$NOW" "$net" "$operator" "$row" "$valid" >>"$LOG"
  done
  update_state "$valid" "$net" "$operator"
  trim_log
  local failing=""
  for row in "${ROWS[@]}"; do
    IFS=$'\t' read -r target kind status _ <<<"$row"
    [[ "$kind" != control && "$status" == fail ]] && failing+=" $target"
  done
  if [[ "$valid" == 0 ]]; then
    say "PROBE_RUN net=$net/$operator: no internet on the phone (a control host is down); the run says nothing about the service"
  elif [[ -n "$failing" ]]; then
    say "PROBE_RUN net=$net/$operator: not answering:$failing"
  else
    say "PROBE_RUN net=$net/$operator: everything answers"
  fi
}

url_host() {
  local rest="${1#*://}"
  rest="${rest%%/*}"
  printf '%s' "${rest%%:*}"
}

summary() {
  local hours="${1:-24}"
  [[ "$hours" =~ ^[0-9]{1,4}$ && "$hours" -ge 1 ]] || stop "usage: vpn_probe.sh summary [hours]"
  [[ -s "$LOG" ]] || stop "no measurements yet: run  bash vpn_probe.sh run"
  awk -F'\t' -v since="$((NOW - hours * 3600))" -v hours="$hours" '
    NR == 1 { next }
    $1 < since { next }
    $12 == 0 { skipped[$1] = 1; next }
    {
      group = $2 "/" $3
      key = group SUBSEP $4
      groups[group] = 1
      targets[group SUBSEP $4] = 1
      if ($6 == "ok") { ok[key]++; sum[key] += $11 } else { bad[key]++; last[key] = $7 }
    }
    END {
      printf "vpn-probe: last %d h; only runs where the control hosts answered\n", hours
      printf "%-22s %-34s %5s %5s %5s %7s  %s\n", "net/operator", "target", "ok", "fail", "ok%", "avg_ms", "last failure"
      n = 0
      for (item in targets) order[++n] = item
      for (i = 1; i <= n; i++) for (j = i + 1; j <= n; j++) if (order[j] < order[i]) { t = order[i]; order[i] = order[j]; order[j] = t }
      for (i = 1; i <= n; i++) {
        key = order[i]
        split(key, part, SUBSEP)
        total = ok[key] + bad[key]
        avg = ok[key] ? int(sum[key] / ok[key]) : 0
        printf "%-22s %-34s %5d %5d %4d%% %7d  %s\n", part[1], part[2], ok[key], bad[key], (100 * ok[key]) / total, avg, last[key]
      }
      skipped_runs = 0
      for (s in skipped) skipped_runs++
      if (skipped_runs) printf "left out: %d runs with no internet on the phone\n", skipped_runs
      if (n == 0) print "no runs in this period"
    }
  ' "$LOG"
}

last_run() {
  [[ -s "$LOG" ]] || stop "no measurements yet: run  bash vpn_probe.sh run"
  awk -F'\t' '
    NR == 1 { next }
    { rows[NR] = $0; ts[NR] = $1; if ($1 > latest) latest = $1 }
    END {
      printf "%-34s %-8s %-9s %5s %7s %7s %7s\n", "target", "status", "reason", "code", "tcp_ms", "tls_ms", "total"
      for (i = 2; i <= NR; i++) {
        if (ts[i] != latest) continue
        split(rows[i], f, "\t")
        printf "%-34s %-8s %-9s %5s %7s %7s %7s\n", f[4], f[6], f[7], f[8], f[9], f[10], f[11]
        net = f[2] "/" f[3]; valid = f[12]
      }
      printf "net %s, run at %s, controls %s\n", net, latest, (valid == 1 ? "answered" : "DOWN")
    }
  ' "$LOG"
}

status() {
  [[ -s "$LOG" ]] || { say "WARN probe: no measurements yet"; return 0; }
  local latest age failing
  latest="$(awk -F'\t' 'NR > 1 && $1 > m { m = $1 } END { print m }' "$LOG")"
  age=$(((NOW - latest) / 60))
  if ((age > 3 * 15)); then
    say "WARN probe: the last run was $age min ago; is the job still scheduled?"
    return 0
  fi
  failing="$(awk -F'\t' -v t="$latest" 'NR > 1 && $1 == t && $5 != "control" && $6 == "fail" && $12 == 1 { printf "%s ", $4 }' "$LOG")"
  if [[ -n "$failing" ]]; then
    say "WARN probe: not answering: $failing(run $age min ago)"
  else
    say "OK probe: last run $age min ago, nothing of ours failing"
  fi
}

show_config() {
  say "PROBE_HOME=$PROBE_HOME"
  say "PROBE_SITE_URL=$PROBE_SITE_URL"
  say "PROBE_ENTRIES=$PROBE_ENTRIES"
  say "PROBE_CONTROLS=$PROBE_CONTROLS"
  say "PROBE_TELEGRAM=$PROBE_TELEGRAM"
  say "PROBE_NET=${PROBE_NET:-(detected)}"
  say "PROBE_OPERATOR=${PROBE_OPERATOR:-(detected)}"
  say "PROBE_TIMEOUT=$PROBE_TIMEOUT"
  say "PROBE_FAIL_STREAK=$PROBE_FAIL_STREAK"
}

load_config
case "${1:-}" in
  run) validate_config; run ;;
  summary) summary "${2:-24}" ;;
  last) last_run ;;
  status) status ;;
  config) show_config ;;
  *) stop "usage: vpn_probe.sh run | summary [hours] | last | status | config" ;;
esac
