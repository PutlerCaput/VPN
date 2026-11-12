#!/usr/bin/env bash
# Stage 2 of retiring the old payment relay between the two servers: remove what
# stage 1 only switched off. Run it on each server (it knows which one it is).
#   bash retire_payment_edge.sh            report: what is left here; changes nothing
#   bash retire_payment_edge.sh --apply    remove it
#
# Removed: the squid package, /etc/node-payment-edge, the old tunnel's config
# (it holds a private key, so it is shredded), the old systemd units; on DE the
# two firewall rules that served the relay; on RU the retired nginx snippet and
# the old capture file.
# Never touched: the wg-db tunnel of the database and its firewall rule (udp
# 51820 from DE to RU), /etc/vpn-db, /var/lib/vpn-db, the VPN entry rules.
# Refuses to run while any of the old services is still active: stage 1 first.
set -euo pipefail

RU_PUBLIC="${RU_PUBLIC:-45.138.157.65}"
DE_PUBLIC="${DE_PUBLIC:-89.125.210.172}"
ETC_DIR="${ETC_DIR:-/etc}"
ROOT_HOME="${ROOT_HOME:-/root}"
CONTAINER="${CONTAINER:-node-rental-bot-1}"
EDGE="$ETC_DIR/node-payment-edge"
OLD_TUNNEL="$ETC_DIR/wireguard/wg-payments.conf"
# ufw rules that existed only for the relay (DE). The udp 51820 rule is NOT here.
UFW_PATTERNS=('18081/tcp on wg-payments' '3128/tcp +ALLOW IN +45\.138\.157\.65')

say() { printf '%s\n' "$*"; }
stop() {
  printf 'RETIRE_STOPPED: %s\n' "$*" >&2
  exit 1
}
as_root() { [[ "$(id -u)" == 0 ]] || stop "run as root"; }
active() { [[ "$(systemctl is-active "$1" 2>/dev/null || true)" == active ]]; }
installed() { dpkg -s "$1" >/dev/null 2>&1; }

side() {
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

ME="$(side)"
if [[ "$ME" == de ]]; then
  UNIT="node-payment-backend.service"
  EXTRA=()
else
  UNIT="node-payment-proxy.service"
  EXTRA=("$ETC_DIR/nginx/pay-callback.conf.retired" "$ROOT_HOME/ru3128.txt")
fi
UNIT_FILE="$ETC_DIR/systemd/system/$UNIT"

ufw_rule_number() {
  # The number of the first rule matching the pattern, or nothing.
  ufw status numbered 2>/dev/null | grep -E "$1" | sed -E 's/^\[ *([0-9]+)\].*/\1/' | head -1
}

still_in_use() {
  # Anything here means stage 1 was not done, or the bot still uses the relay.
  local name workdir
  for name in "$UNIT" wg-quick@wg-payments squid; do
    if active "$name"; then
      printf '%s is still running; ' "$name"
    fi
  done
  if [[ "$ME" == de ]] && command -v docker >/dev/null 2>&1; then
    workdir="$(docker inspect -f '{{ index .Config.Labels "com.docker.compose.project.working_dir" }}' "$CONTAINER" 2>/dev/null || true)"
    if [[ -n "$workdir" && -f "$workdir/.env" ]] \
      && grep -qE '^(YOOKASSA_PROXY_URL=.|YOOKASSA_EGRESS=ru_gateway|TELEGRAM_PROXY_URL=.)' "$workdir/.env"; then
      printf 'the bot is still set to use the relay or the proxy (.env); '
    fi
  fi
}

list_left() {
  local path pattern
  for path in "$EDGE" "$OLD_TUNNEL" "$UNIT_FILE" ${EXTRA[@]+"${EXTRA[@]}"}; do
    if [[ -e "$path" ]]; then say "  to remove: $path"; fi
  done
  if installed squid; then say "  to remove: the squid package"; fi
  if [[ "$ME" == de ]]; then
    for pattern in "${UFW_PATTERNS[@]}"; do
      if [[ -n "$(ufw_rule_number "$pattern")" ]]; then
        say "  to remove: firewall rule matching '$pattern'"
      fi
    done
  fi
}

report() {
  local busy left
  say "== Old payment relay on this server ($ME)"
  busy="$(still_in_use)"
  left="$(list_left)"
  if [[ -n "$busy" ]]; then say "  IN USE: ${busy%; }"; fi
  if [[ -n "$left" ]]; then
    say "$left"
  else
    say "  nothing is left"
  fi
  say "RETIRE_REPORT_DONE"
}

apply() {
  as_root
  local busy pattern number path
  busy="$(still_in_use)"
  [[ -z "$busy" ]] || stop "${busy%; }. Nothing was removed"
  if [[ "$ME" == de ]]; then
    for pattern in "${UFW_PATTERNS[@]}"; do
      # Numbers shift after each removal: look the rule up again every time.
      while number="$(ufw_rule_number "$pattern")" && [[ -n "$number" ]]; do
        ufw --force delete "$number" >/dev/null || stop "could not remove firewall rule $number"
        say "removed: firewall rule matching '$pattern'"
      done
    done
  fi
  if installed squid; then
    DEBIAN_FRONTEND=noninteractive apt-get purge -y -q squid squid-common >/dev/null \
      || stop "apt could not remove squid"
    say "removed: the squid package"
  fi
  if [[ -f "$OLD_TUNNEL" ]]; then
    # It holds the old tunnel's private key.
    shred -u "$OLD_TUNNEL" || stop "could not destroy $OLD_TUNNEL"
    say "removed: $OLD_TUNNEL (shredded)"
  fi
  for path in "$EDGE" "$UNIT_FILE" ${EXTRA[@]+"${EXTRA[@]}"}; do
    if [[ -e "$path" ]]; then
      rm -rf -- "$path"
      say "removed: $path"
    fi
  done
  systemctl daemon-reload >/dev/null 2>&1 || true
  say "RETIRE_DONE ($ME): nothing of the old payment relay is left here."
}

case "${1:-}" in
  "") report ;;
  --apply) apply ;;
  *) stop "usage: retire_payment_edge.sh [--apply]" ;;
esac
