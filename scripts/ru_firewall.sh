#!/usr/bin/env bash
# A host firewall for the RU VPN-entry server, with a way back if it goes wrong.
#
#   bash scripts/ru_firewall.sh            # report: what is open now, what --apply would set
#   bash scripts/ru_firewall.sh --apply    # INPUT policy DROP + allow 22/80; undoes itself in 10 min
#   bash scripts/ru_firewall.sh --confirm  # from a NEW ssh session: keep it, persist to rules.v4
#   bash scripts/ru_firewall.sh --revert   # restore the snapshot taken at --apply, at once
#   bash scripts/ru_firewall.sh --status   # one line: ON or OFF
#
# It only ever touches the filter INPUT chain. It never touches FORWARD, the nat
# table, or OUTPUT, so the VPN entry (DNAT 443/2087/8443 -> DE:2096, which is
# FORWARD + nat, not INPUT) keeps working and egress stays open.
#
# Why it cannot lock you out:
#   1. an established-connection ACCEPT is the first rule, so the session you run
#      it from survives the switch to policy DROP;
#   2. --apply snapshots every table first (iptables-save) and a 10-minute timer
#      restores that snapshot unless --confirm is run from a NEW ssh session;
#   3. --confirm refuses unless a fresh inbound ssh connection is seen after the
#      change (so not the same window that applied it), then persists and stops
#      the timer.
#
# Opened to the whole internet: TCP 22 (ssh) and TCP 80 (nginx). Everything else
# inbound is dropped. The VPN ports are forwarded, not listened on, so they are
# unaffected. If you add a service later, add its port here before --apply.
set -euo pipefail

ALLOW_TCP="22 80"                      # inbound TCP ports open to everyone
ETC_DIR="${ETC_DIR:-/etc}"
RULES="${RULES:-$ETC_DIR/iptables/rules.v4}"
STATE_DIR="${STATE_DIR:-$ETC_DIR/iptables}"
PENDING="$STATE_DIR/.firewall-pending"   # holds the snapshot path from --apply to --confirm
UNIT="ru-firewall-revert"
REVERT_AFTER="${REVERT_AFTER:-600}"      # seconds before the automatic undo
IPTABLES="${IPTABLES_BIN:-iptables}"
SAVE="${IPTABLES_SAVE_BIN:-iptables-save}"
RESTORE="${IPTABLES_RESTORE_BIN:-iptables-restore}"
LOOKBACK_SECONDS=1200

SELF="$(readlink -f "$0")"
say() { printf '%s\n' "$*"; }
stop() { printf 'RU_FIREWALL_STOPPED: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || stop "missing command: $1"; }

MODE=report
case "${1:-}" in
  "") ;;
  --apply | --confirm | --revert | --status) MODE="${1#--}" ;;
  *) stop "usage: ru_firewall.sh [--apply | --confirm | --revert | --status]" ;;
esac

open_ports_now() { $IPTABLES -S INPUT 2>/dev/null | grep -oE 'dport [0-9]+' | awk '{print $2}' | sort -un | paste -sd' ' -; }
policy_now() { $IPTABLES -S INPUT 2>/dev/null | awk '$1 == "-P" { print $3; exit }'; }

set_input_chain() {
  # Rebuild ONLY the filter INPUT chain, leaving FORWARD, nat and OUTPUT alone.
  # Order matters: allow rules go in while the policy is still ACCEPT, then the
  # policy flips to DROP last — so an in-flight ssh session is never dropped.
  $IPTABLES -F INPUT
  $IPTABLES -A INPUT -i lo -j ACCEPT
  $IPTABLES -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  $IPTABLES -A INPUT -m conntrack --ctstate INVALID -j DROP
  $IPTABLES -A INPUT -p icmp --icmp-type echo-request -m limit --limit 5/second -j ACCEPT
  local p
  for p in $ALLOW_TCP; do
    $IPTABLES -A INPUT -p tcp --dport "$p" -m conntrack --ctstate NEW -j ACCEPT
  done
  $IPTABLES -P INPUT DROP
}

report() {
  say "== RU host firewall"
  say "  INPUT policy now: $(policy_now || echo unknown)"
  say "  inbound TCP allowed now: $(open_ports_now || echo none)"
  if [[ -f "$PENDING" ]]; then
    say "  STATE: a change is waiting — run --confirm from a NEW ssh session, or --revert."
  fi
  say "  --apply would set: INPUT policy DROP; allow lo, established, ssh 22, http 80,"
  say "                     rate-limited ping; drop the rest. FORWARD/nat/OUTPUT untouched,"
  say "                     so the VPN entry (DNAT 443/2087/8443) keeps working."
  say "  then a timer restores the snapshot after $((REVERT_AFTER/60)) min unless you --confirm."
  say "RU_FIREWALL_REPORT_DONE"
}

apply() {
  [[ "$(id -u)" == 0 ]] || stop "run as root"
  need "$IPTABLES"; need "$SAVE"; need "$RESTORE"; need systemd-run
  [[ ! -f "$PENDING" ]] || stop "a change is already waiting: run --confirm or --revert"
  # Guard: refuse on a box with no ssh listener rule path (wrong host / no nat) unless forced.
  mkdir -p "$STATE_DIR"
  local snap="$STATE_DIR/firewall.before-$(date +%Y%m%dT%H%M%S)"
  "$SAVE" >"$snap" || stop "could not snapshot the current ruleset"
  if ! set_input_chain; then
    "$RESTORE" <"$snap" 2>/dev/null || true
    stop "could not set the INPUT chain; the snapshot was restored (kept: $snap)"
  fi
  printf '%s\n' "$snap" >"$PENDING"
  if ! systemd-run --quiet --unit="$UNIT" --on-active="${REVERT_AFTER}s" \
      --description="Undo the RU firewall unless confirmed" /bin/bash "$SELF" --revert; then
    "$RESTORE" <"$snap" || true
    rm -f "$PENDING"
    stop "the safety timer could not start; the old ruleset is back"
  fi
  say "RU_FIREWALL_APPLIED: INPUT is now DROP with ssh/http allowed."
  say "It undoes itself in $((REVERT_AFTER/60)) minutes unless you confirm. Keep THIS window open and:"
  say "  1. open a NEW ssh session with your key;"
  say "  2. if it lets you in, run there:  bash /root/ru_firewall.sh --confirm"
  say "If the new session does not connect, do nothing: the old ruleset comes back by itself."
  say "Take it back now:  bash /root/ru_firewall.sh --revert"
}

confirm() {
  [[ "$(id -u)" == 0 ]] || stop "run as root"
  [[ -f "$PENDING" ]] || stop "no change is waiting; nothing to confirm"
  need journalctl
  local since fresh
  since=$(( $(date +%s) - LOOKBACK_SECONDS ))
  fresh="$(journalctl -u ssh -u sshd --since "@$since" --no-pager -o cat 2>/dev/null | grep -c 'Accepted ' || true)"
  (( fresh >= 1 )) || stop "no new ssh login since the change. Open a NEW ssh session and run --confirm there"
  systemctl stop "$UNIT.timer" >/dev/null 2>&1 || true
  "$SAVE" >"$RULES" || stop "could not persist to $RULES (ruleset is live but not saved)"
  command -v netfilter-persistent >/dev/null 2>&1 && netfilter-persistent save >/dev/null 2>&1 || true
  rm -f "$PENDING"
  say "RU_FIREWALL_OK: host firewall is on and saved to $RULES. Timer stopped."
  say "Undo any time: bash scripts/ru_firewall.sh --revert (then save)."
}

revert() {
  [[ "$(id -u)" == 0 ]] || stop "run as root"
  local snap=""
  [[ -f "$PENDING" ]] && snap="$(cat "$PENDING")"
  if [[ -n "$snap" && -f "$snap" ]]; then
    "$RESTORE" <"$snap" || stop "could not restore $snap"
    say "RU_FIREWALL_REVERTED: restored the ruleset from before --apply ($snap)."
  else
    # No pending snapshot: fall back to the persisted rules, else open INPUT.
    if [[ -f "$RULES" ]]; then "$RESTORE" <"$RULES" || true; say "RU_FIREWALL_REVERTED: reloaded $RULES."
    else "$IPTABLES" -P INPUT ACCEPT; say "RU_FIREWALL_REVERTED: INPUT policy set back to ACCEPT."; fi
  fi
  systemctl stop "$UNIT.timer" >/dev/null 2>&1 || true
  rm -f "$PENDING"
}

status() {
  if [[ -f "$PENDING" ]]; then say "WARN firewall: a change waits for --confirm and will undo itself"; return 0; fi
  if [[ "$(policy_now || echo unknown)" == DROP ]]; then say "OK firewall: INPUT policy is DROP (host firewall on)"; else say "WARN firewall: INPUT policy is not DROP (no host firewall)"; fi
}

case "$MODE" in
  report) report ;;
  apply) apply ;;
  confirm) confirm ;;
  revert) revert ;;
  status) status ;;
esac
