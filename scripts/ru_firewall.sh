#!/usr/bin/env bash
# Host firewall for the RU VPN-entry server, with a way back if it goes wrong.
#   bash ru_firewall.sh            report: what is open now and what --apply would set
#   bash ru_firewall.sh --apply    INPUT policy DROP + allow 22/80; undoes itself in 10 min
#   bash ru_firewall.sh --confirm  from a NEW ssh login: keep it and save to rules.v4
#   bash ru_firewall.sh --revert   restore the snapshot taken at --apply, at once
#   bash ru_firewall.sh --status   one line: OK or WARN
#
# Touches ONLY the filter INPUT chain: never FORWARD, nat or OUTPUT. The VPN entry
# (DNAT 443/2087/8443 -> DE:2096) is forwarded traffic, so it is unaffected.
# IPv4 only (RU has no IPv6 address).
#
# Cannot lock you out: the ESTABLISHED rule goes in before the policy flips to DROP
# (your session survives); --apply snapshots the ruleset and a 10-minute timer
# restores it; --confirm needs an ssh login made AFTER --apply (not the same window).
set -euo pipefail

ALLOW_TCP="22 80"                        # inbound TCP ports open to everyone
ETC_DIR="${ETC_DIR:-/etc}"
RULES="${RULES:-$ETC_DIR/iptables/rules.v4}"
STATE_DIR="${STATE_DIR:-$ETC_DIR/iptables}"
PENDING="$STATE_DIR/.firewall-pending"   # line 1: snapshot path, line 2: apply time (epoch)
UNIT="ru-firewall-revert"
REVERT_AFTER="${REVERT_AFTER:-600}"      # seconds before the automatic undo
IPTABLES="${IPTABLES_BIN:-iptables}"
SAVE="${IPTABLES_SAVE_BIN:-iptables-save}"
RESTORE="${IPTABLES_RESTORE_BIN:-iptables-restore}"

SELF="$(readlink -f "$0")"
say() { printf '%s\n' "$*"; }
stop() { printf 'RU_FIREWALL_STOPPED: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || stop "missing command: $1"; }
as_root() { [[ "$(id -u)" == 0 ]] || stop "run as root"; }

MODE=report
case "${1:-}" in
  "") ;;
  --apply | --confirm | --revert | --status) MODE="${1#--}" ;;
  *) stop "usage: ru_firewall.sh [--apply | --confirm | --revert | --status]" ;;
esac

open_ports_now() { $IPTABLES -S INPUT 2>/dev/null | grep -oE 'dport [0-9]+' | awk '{print $2}' | sort -un | paste -sd' ' -; }
policy_now() { $IPTABLES -S INPUT 2>/dev/null | awk '$1 == "-P" { print $3; exit }'; }

set_input_chain() {
  # Rebuild only INPUT. Allow rules go in while the policy is still ACCEPT and the
  # policy flips to DROP last, so an in-flight ssh session is never dropped.
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
  say "  inbound TCP rules now: $(open_ports_now || echo none)"
  if [[ -f "$PENDING" ]]; then
    say "  STATE: a change is waiting: run --confirm from a NEW ssh login, or --revert."
  fi
  say "  --apply would set: INPUT policy DROP; allow lo, established, ssh 22, http 80,"
  say "                     rate-limited ping; drop the rest. FORWARD/nat/OUTPUT untouched,"
  say "                     so the VPN entry (DNAT 443/2087/8443) keeps working."
  say "  then a timer restores the snapshot after $((REVERT_AFTER / 60)) min unless you --confirm."
  say "RU_FIREWALL_REPORT_DONE"
}

apply() {
  as_root
  need "$IPTABLES"; need "$SAVE"; need "$RESTORE"; need systemd-run
  [[ ! -f "$PENDING" ]] || stop "a change is already waiting: run --confirm or --revert"
  mkdir -p "$STATE_DIR"
  local snap
  snap="$STATE_DIR/firewall.before-$(date +%Y%m%dT%H%M%S)"
  "$SAVE" >"$snap" || stop "could not snapshot the current ruleset"
  if ! set_input_chain; then
    "$RESTORE" <"$snap" 2>/dev/null || true
    stop "could not set the INPUT chain; the snapshot was restored (kept: $snap)"
  fi
  printf '%s\n%s\n' "$snap" "$(date +%s)" >"$PENDING"
  if ! systemd-run --quiet --unit="$UNIT" --on-active="${REVERT_AFTER}s" \
      --description="Undo the RU firewall unless confirmed" /bin/bash "$SELF" --revert; then
    "$RESTORE" <"$snap" || true
    rm -f "$PENDING"
    stop "the safety timer could not start; the old ruleset is back"
  fi
  say "RU_FIREWALL_APPLIED: INPUT is now DROP with ssh/http allowed."
  say "It undoes itself in $((REVERT_AFTER / 60)) minutes unless you confirm. Keep THIS window open and:"
  say "  1. open a NEW ssh login with your key;"
  say "  2. if it lets you in, run there:  bash $SELF --confirm"
  say "If the new login does not connect, do nothing: the old ruleset comes back by itself."
  say "Take it back now:  bash $SELF --revert"
}

confirm() {
  as_root
  [[ -f "$PENDING" ]] || stop "no change is waiting; nothing to confirm"
  need journalctl
  local since fresh
  since="$(sed -n 2p "$PENDING")"
  [[ "$since" =~ ^[0-9]+$ ]] || stop "$PENDING is damaged: run --revert"
  fresh="$(journalctl -u ssh -u sshd --since "@$since" --no-pager -o cat 2>/dev/null | grep -c 'Accepted ' || true)"
  ((fresh >= 1)) || stop "no ssh login since the change. Open a NEW ssh login and run --confirm there"
  "$SAVE" >"$RULES" || stop "could not save to $RULES (the ruleset is live but not saved)"
  systemctl stop "$UNIT.timer" >/dev/null 2>&1 || true
  rm -f "$PENDING"
  say "RU_FIREWALL_OK: host firewall is on and saved to $RULES. The timer is stopped."
  say "Take it back any time: bash $SELF --revert (it saves the open state too)."
}

revert() {
  as_root
  if [[ -f "$PENDING" ]]; then
    # Within the 10 minutes (timer or by hand): put back the full snapshot from --apply.
    local snap
    snap="$(sed -n 1p "$PENDING")"
    [[ -n "$snap" && -f "$snap" ]] || stop "the snapshot named in $PENDING is missing; open INPUT by hand"
    "$RESTORE" <"$snap" || stop "could not restore $snap"
    say "RU_FIREWALL_REVERTED: restored the ruleset from before --apply ($snap)."
  else
    # After --confirm: open INPUT again (touches nothing else) and save, so a reboot
    # does not bring the firewall back.
    $IPTABLES -P INPUT ACCEPT
    $IPTABLES -F INPUT
    "$SAVE" >"$RULES" || stop "INPUT is open but could not be saved to $RULES"
    say "RU_FIREWALL_REVERTED: INPUT is open again and saved to $RULES."
  fi
  systemctl stop "$UNIT.timer" >/dev/null 2>&1 || true
  rm -f "$PENDING"
}

status() {
  if [[ -f "$PENDING" ]]; then
    say "WARN firewall: a change waits for --confirm and will undo itself"
  elif [[ "$(policy_now || echo unknown)" == DROP ]]; then
    say "OK firewall: INPUT policy is DROP (host firewall on)"
  else
    say "WARN firewall: INPUT policy is not DROP (no host firewall)"
  fi
}

case "$MODE" in
  report) report ;;
  apply) apply ;;
  confirm) confirm ;;
  revert) revert ;;
  status) status ;;
esac
