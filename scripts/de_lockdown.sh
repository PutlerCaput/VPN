#!/usr/bin/env bash
# Host firewall (ufw) for the DE exit server: only the ports the service needs,
# the Hiddify panel no longer reachable from the internet. PREPARED, NOT APPLIED:
# the owner runs it by hand (docs/PANEL_LOCKDOWN.md), never a deploy script.
#
#   bash de_lockdown.sh             report: what is open now, the exact ufw rules
#                                   --apply would set (changes nothing)
#   bash de_lockdown.sh --apply     backup of /etc/ufw, allow rules FIRST, then
#                                   default deny incoming; undoes itself in 10 min
#   bash de_lockdown.sh --confirm   from a NEW ssh login: keep it
#   bash de_lockdown.sh --revert    restore the backup taken at --apply, at once
#
# Kept open (override with the variables below):
#   TCP  SSH (port from sshd -T), 80 (ACME), 443 (site), 8443 (subscription),
#        2443 (VLESS REALITY direct entry)
#   UDP  36099 (Hysteria2), 39051 44877 (AmneziaWG)
#   from RU: all UDP (the wg-db and wg-vpn tunnels, whichever side starts the
#        handshake); everything arriving on the wg-vpn interface (the RU
#        relay's TCP entry, scripts/ru_vless_relay.sh)
# Forwarding stays allowed ("default allow routed"): AmneziaWG clients are
# routed through this host, a DROP there would cut them off.
# The panel itself is a hostname on Caddy's 8443, so ufw cannot tell it from
# the subscription: the Caddy part (allowlist on the panel site) is a separate,
# printed snippet, see docs/PANEL_LOCKDOWN.md.
#
# Docker note: ports published by containers (-p) go through Docker's own
# iptables chains and are NOT filtered by ufw. The report lists them.
set -euo pipefail

ALLOW_TCP="${ALLOW_TCP-80 443 8443 2443}"
ALLOW_UDP="${ALLOW_UDP-36099 39051 44877}"
# RU relays and their tunnel interfaces, space separated (a second RU entry:
# RU_PEER="45.138.157.65 <RU-2>" TUNNEL_IF="wg-vpn wg-vpn2").
RU_PEER="${RU_PEER-45.138.157.65}"
TUNNEL_IF="${TUNNEL_IF-wg-vpn}"
# Extra addresses allowed to everything (an admin's fixed IP), space separated.
ADMIN_IPS="${ADMIN_IPS-}"
ETC_DIR="${ETC_DIR:-/etc}"
STATE_DIR="${STATE_DIR:-/root/de-lockdown}"
PENDING="$STATE_DIR/pending"   # line 1: backup dir, line 2: ufw was active (1/0)
UNIT="de-lockdown-revert"
REVERT_AFTER="${REVERT_AFTER:-600}"
UFW="${UFW_BIN:-ufw}"
AS_ROOT_CHECK="${AS_ROOT_CHECK:-1}"

SELF="$(readlink -f "$0")"
say() { printf '%s\n' "$*"; }
stop() { printf 'DE_LOCKDOWN_STOPPED: %s\n' "$*" >&2; exit 1; }
as_root() { [[ "$AS_ROOT_CHECK" == 0 || "$(id -u)" == 0 ]] || stop "run as root"; }

MODE=report
case "${1:-}" in
  "") ;;
  --apply | --confirm | --revert) MODE="${1#--}" ;;
  *) stop "usage: de_lockdown.sh [--apply | --confirm | --revert]" ;;
esac

valid() {
  local p
  for p in $ALLOW_TCP $ALLOW_UDP; do
    [[ "$p" =~ ^[0-9]{1,5}$ && "$p" -ge 1 && "$p" -le 65535 ]] || stop "not a port: $p"
  done
  for p in $RU_PEER $ADMIN_IPS; do
    [[ "$p" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}(/[0-9]{1,2})?$ ]] || stop "not an IPv4 address: $p"
  done
  for p in $TUNNEL_IF; do
    [[ "$p" =~ ^[A-Za-z0-9_.-]{1,15}$ ]] || stop "not an interface name: $p"
  done
}

ssh_port() {
  local port
  port="$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2; exit}')" || true
  [[ "$port" =~ ^[0-9]{1,5}$ ]] || port=22
  printf '%s' "$port"
}

# The rules, one ufw command per line (without the leading "ufw").
plan() {
  local p
  say "allow $(ssh_port)/tcp comment 'ssh'"
  for p in $ADMIN_IPS; do say "allow from $p comment 'admin'"; done
  for p in $ALLOW_TCP; do say "allow $p/tcp comment 'de-lockdown'"; done
  for p in $ALLOW_UDP; do say "allow $p/udp comment 'de-lockdown'"; done
  for p in $RU_PEER; do say "allow proto udp from $p to any comment 'RU tunnels'"; done
  for p in $TUNNEL_IF; do say "allow in on $p comment 'RU relay via tunnel'"; done
  say "default allow routed"
  say "default allow outgoing"
  say "default deny incoming"
}

report() {
  say "== ufw now"
  $UFW status verbose 2>/dev/null || say "(ufw not available)"
  say "== listening (public) sockets"
  ss -H -lntu 2>/dev/null | awk '{print $1, $5}' | grep -vE ' (127\.|\[::1\])' | sort -u || true
  say "== container ports (NOT filtered by ufw)"
  if command -v docker >/dev/null 2>&1; then
    docker ps --format '{{.Names}} {{.Ports}}' 2>/dev/null || true
  fi
  say "== --apply would run (ufw ...):"
  plan | sed 's/^/  ufw /'
  say "DE_LOCKDOWN_PLAN: nothing changed. Caddy panel allowlist: docs/PANEL_LOCKDOWN.md"
}

apply() {
  as_root
  [[ ! -f "$PENDING" ]] || stop "an apply is pending: --confirm or --revert first"
  mkdir -p "$STATE_DIR"
  local backup active=0 line
  backup="$STATE_DIR/ufw-$(date +%Y%m%d-%H%M%S)"
  cp -a "$ETC_DIR/ufw" "$backup"
  $UFW status 2>/dev/null | grep -q 'Status: active' && active=1
  printf '%s\n%s\n' "$backup" "$active" > "$PENDING"
  # The undo is armed before anything changes.
  if command -v systemd-run >/dev/null 2>&1; then
    systemd-run --unit "$UNIT" --on-active="$REVERT_AFTER" /bin/bash "$SELF" --revert >/dev/null \
      || stop "could not arm the automatic undo; nothing changed"
  else
    stop "systemd-run is missing: no automatic undo, refusing to apply"
  fi
  # Allow rules first: the default policy flips last, the ssh session survives.
  while IFS= read -r line; do
    # shellcheck disable=SC2086
    eval "$UFW $line" >/dev/null || { bash "$SELF" --revert; stop "ufw $line failed; reverted"; }
  done < <(plan)
  $UFW --force enable >/dev/null || { bash "$SELF" --revert; stop "ufw enable failed; reverted"; }
  say "DE_LOCKDOWN_APPLIED: backup $backup. Open a NEW ssh login within $((REVERT_AFTER / 60)) min and run --confirm, or it reverts itself."
}

confirm() {
  as_root
  [[ -f "$PENDING" ]] || stop "nothing to confirm"
  systemctl stop "$UNIT.timer" "$UNIT.service" >/dev/null 2>&1 || true
  rm -f "$PENDING"
  say "DE_LOCKDOWN_OK: rules kept."
}

revert() {
  as_root
  [[ -f "$PENDING" ]] || stop "nothing to revert (no pending apply; old backups are in $STATE_DIR)"
  local backup active
  backup="$(sed -n 1p "$PENDING")"
  active="$(sed -n 2p "$PENDING")"
  [[ -d "$backup" ]] || stop "backup $backup is missing"
  rm -rf "$ETC_DIR/ufw.lockdown-failed"
  cp -a "$ETC_DIR/ufw" "$ETC_DIR/ufw.lockdown-failed"
  rm -rf "$ETC_DIR/ufw" && cp -a "$backup" "$ETC_DIR/ufw"
  if [[ "$active" == 1 ]]; then $UFW reload >/dev/null || true; else $UFW --force disable >/dev/null || true; fi
  systemctl stop "$UNIT.timer" >/dev/null 2>&1 || true
  rm -f "$PENDING"
  say "DE_LOCKDOWN_REVERTED: /etc/ufw is back to $backup"
}

valid
case "$MODE" in
  report) report ;;
  apply) apply ;;
  confirm) confirm ;;
  revert) revert ;;
esac
