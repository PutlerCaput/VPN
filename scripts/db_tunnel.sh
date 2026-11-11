#!/usr/bin/env bash
# WireGuard tunnel between the two servers for the database gateway (wg-db).
#   bash db_tunnel.sh                    report: this side's state; changes nothing
#   bash db_tunnel.sh --key              create this side's key pair if missing,
#                                        print the PUBLIC key
#   bash db_tunnel.sh --up <public key>  the OTHER side's public key: write the
#                                        config, start the tunnel, start it on boot
#   bash db_tunnel.sh --down             stop it and keep it off on boot (keys stay)
#   bash db_tunnel.sh --status           one line: OK or WARN
#
# Run it on both servers. It knows which one it is by the server's own address:
# RU listens on udp 51820, DE dials out and keeps the tunnel alive.
# A private key is created where it is used and is never printed or copied;
# only public keys travel between the servers.
set -euo pipefail

IFACE="wg-db"
RU_PUBLIC="${RU_PUBLIC:-45.138.157.65}"
DE_PUBLIC="${DE_PUBLIC:-89.125.210.172}"
PORT="${DB_TUNNEL_PORT:-51820}"
RU_ADDR="10.204.0.1"
DE_ADDR="10.204.0.2"
WG_DIR="${WG_DIR:-/etc/wireguard}"
KEY="${WG_DIR}/${IFACE}.key"
PUB="${WG_DIR}/${IFACE}.pub"
CONF="${WG_DIR}/${IFACE}.conf"
UNIT="wg-quick@$IFACE"
STALE_AFTER=180 # seconds without a handshake before the tunnel counts as down

say() { printf '%s\n' "$*"; }
stop() {
  printf 'DB_TUNNEL_STOPPED: %s\n' "$*" >&2
  exit 1
}
need() { command -v "$1" >/dev/null 2>&1 || stop "missing command: $1 (apt-get install -y wireguard-tools)"; }
as_root() { [[ "$(id -u)" == 0 ]] || stop "run as root"; }

side() {
  # SIDE=ru|de overrides the detection (tests, or a server behind another address).
  if [[ -n "${SIDE:-}" ]]; then
    [[ "$SIDE" == ru || "$SIDE" == de ]] || stop "SIDE must be ru or de"
    printf '%s' "$SIDE"
    return
  fi
  local addresses
  addresses="$(ip -4 -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1)"
  if grep -qxF "$RU_PUBLIC" <<<"$addresses"; then
    printf ru
  elif grep -qxF "$DE_PUBLIC" <<<"$addresses"; then
    printf de
  else
    stop "this server has neither $RU_PUBLIC nor $DE_PUBLIC: run it on RU or DE"
  fi
}

handshake_age() {
  # Seconds since the last handshake; empty when there is none yet.
  local at
  at="$(wg show "$IFACE" latest-handshakes 2>/dev/null | awk 'NR == 1 { print $2 }')"
  [[ "$at" =~ ^[0-9]+$ && "$at" -gt 0 ]] || return 0
  printf '%s' "$(($(date +%s) - at))"
}

report() {
  local me other age
  me="$(side)"
  [[ "$me" == ru ]] && other="$DE_ADDR" || other="$RU_ADDR"
  say "== Database tunnel ($IFACE), this server: $me"
  if command -v wg >/dev/null 2>&1; then
    say "  wireguard-tools: installed"
  else
    say "  wireguard-tools: MISSING (apt-get install -y wireguard-tools)"
  fi
  if [[ -s "$PUB" ]]; then
    say "  this side's public key: $(cat "$PUB")"
  else
    say "  this side's key: not created yet (--key)"
  fi
  if [[ -f "$CONF" ]]; then
    say "  config: present ($CONF)"
  else
    say "  config: not written yet (--up <the other side's public key>)"
  fi
  if ip link show "$IFACE" >/dev/null 2>&1; then
    age="$(handshake_age)"
    say "  interface: up; last handshake: ${age:+$age s ago}${age:-none yet}"
    if ping -c 1 -W 2 "$other" >/dev/null 2>&1; then
      say "  ping $other: answers"
    else
      say "  ping $other: no answer"
    fi
  else
    say "  interface: down"
  fi
  say "DB_TUNNEL_REPORT_DONE"
}

make_key() {
  as_root
  need wg
  umask 077
  mkdir -p "$WG_DIR"
  if [[ ! -s "$KEY" ]]; then
    wg genkey >"$KEY"
  fi
  wg pubkey <"$KEY" >"$PUB"
  chmod 600 "$KEY"
  chmod 644 "$PUB"
  say "DB_TUNNEL_PUBLIC_KEY $(side) $(cat "$PUB")"
  say "Give this line's key to the other server:  bash db_tunnel.sh --up <key>"
}

bring_up() {
  as_root
  need wg
  need wg-quick
  local peer="$1" me taken
  [[ "$peer" =~ ^[A-Za-z0-9+/]{43}=$ ]] || stop "that is not a WireGuard public key (44 characters ending with =)"
  [[ -s "$KEY" && -s "$PUB" ]] || stop "this side has no key yet: run --key first"
  [[ "$peer" != "$(cat "$PUB")" ]] || stop "that is THIS server's own key; give the OTHER server's key"
  me="$(side)"
  # Another interface already using the tunnel's addresses would steal the route.
  taken="$(ip -4 -o addr show 2>/dev/null | awk -v own="$IFACE" '$2 != own && $4 ~ /^10\.204\.0\./ { print $2 }' | paste -sd' ' -)"
  [[ -z "$taken" ]] || stop "10.204.0.x is already used by: $taken"
  if [[ "$me" == ru ]] && ! ip link show "$IFACE" >/dev/null 2>&1 \
    && ss -H -lun 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$PORT\$"; then
    stop "udp $PORT is already taken on this server (an old tunnel?): stop it first"
  fi
  umask 077
  {
    printf '# Database tunnel, written by scripts/db_tunnel.sh. Do not copy this file.\n'
    printf '[Interface]\n'
    if [[ "$me" == ru ]]; then
      printf 'Address = %s/30\nListenPort = %s\n' "$RU_ADDR" "$PORT"
    else
      printf 'Address = %s/30\n' "$DE_ADDR"
    fi
    printf 'PrivateKey = %s\n\n[Peer]\nPublicKey = %s\n' "$(cat "$KEY")" "$peer"
    if [[ "$me" == ru ]]; then
      printf 'AllowedIPs = %s/32\n' "$DE_ADDR"
    else
      printf 'AllowedIPs = %s/32\nEndpoint = %s:%s\nPersistentKeepalive = 25\n' \
        "$RU_ADDR" "$RU_PUBLIC" "$PORT"
    fi
  } >"$CONF.new"
  chmod 600 "$CONF.new"
  mv "$CONF.new" "$CONF"
  systemctl enable "$UNIT" >/dev/null 2>&1 || stop "could not enable $UNIT"
  systemctl restart "$UNIT" || stop "could not start $UNIT (journalctl -u $UNIT -n 20)"
  say "DB_TUNNEL_UP: $IFACE is configured on $me and starts on boot."
  say "When both sides are up, check:  bash db_tunnel.sh --status"
}

bring_down() {
  as_root
  systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
  say "DB_TUNNEL_DOWN: $IFACE is stopped and stays off on boot. Keys and config are kept."
}

status() {
  local age
  if ! ip link show "$IFACE" >/dev/null 2>&1; then
    say "WARN tunnel: $IFACE is down"
    return
  fi
  age="$(handshake_age)"
  if [[ -z "$age" ]]; then
    say "WARN tunnel: $IFACE is up but the other side has not answered yet"
  elif ((age > STALE_AFTER)); then
    say "WARN tunnel: last handshake $age s ago"
  else
    say "OK tunnel: $IFACE is up, last handshake $age s ago"
  fi
}

case "${1:-}" in
  "") report ;;
  --key) make_key ;;
  --up)
    [[ $# -eq 2 ]] || stop "usage: db_tunnel.sh --up <the other side's public key>"
    bring_up "$2"
    ;;
  --down) bring_down ;;
  --status) status ;;
  *) stop "usage: db_tunnel.sh [--key | --up <public key> | --down | --status]" ;;
esac
