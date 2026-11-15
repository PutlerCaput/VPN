#!/usr/bin/env bash
# The address subscribers' AmneziaWG configs connect to (Endpoint). Runs on DE.
#
#   bash awg_endpoint.sh                 the address new configs get now
#   bash awg_endpoint.sh --set HOST      new configs get HOST (the RU relay:
#                                        45.138.157.65); AWG restarts once
#
# The port stays the same. Keys, devices and the bot are not changed. Configs
# already handed out keep the old address until the subscriber takes a new one
# in the bot (/amnezia). If the container does not come back healthy, the
# previous setting is put back. Set the relay up on RU first:
# scripts/ru_awg_relay.sh --apply.
set -euo pipefail

AWG_DIR="${AWG_DIR:-/root/cyberdefency-awg}"
AWG_NAME="${AWG_NAME:-cyberdefency-awg}"
ENV_FILE="$AWG_DIR/.env"
WAIT_SECONDS="${WAIT_SECONDS:-120}"
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SRC="$(dirname "$HERE")"
RELEASE_GUARD="${RELEASE_GUARD_BIN:-/usr/local/sbin/vpn-release-guard}"

say() { printf '%s\n' "$*"; }
stop() {
  printf 'AWG_ENDPOINT_STOPPED: %s\n' "$*" >&2
  exit 1
}

[[ -f "$ENV_FILE" ]] || stop "$ENV_FILE not found: is AmneziaWG installed (install_awg.sh)?"
current="$(sed -n 's/^AWG_ENDPOINT_HOST=//p' "$ENV_FILE" | head -1)"
port="$(sed -n 's/^AWG_PORT=//p' "$ENV_FILE" | head -1)"

if [[ "${1:-}" != "--set" ]]; then
  say "new configs connect to: ${current:-?}:${port:-?}"
  running="$(docker exec "$AWG_NAME" printenv AWG_ENDPOINT_HOST 2>/dev/null || echo '?')"
  say "the running container uses: $running"
  exit 0
fi

if [[ -x "$RELEASE_GUARD" ]]; then
  "$RELEASE_GUARD" --verify-head "$SRC" || exit 1
fi
host="${2:-}"
[[ "$host" =~ ^[A-Za-z0-9.-]+$ ]] || stop "usage: --set HOST (an IPv4 address or a name)"
case "$host" in
  10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*|127.*)
    stop "$host is a private address" ;;
esac
if [[ "$host" == "$current" ]]; then
  say "AWG_ENDPOINT_UNCHANGED: new configs already connect to $host:$port"
  exit 0
fi

backup="$ENV_FILE.before-endpoint-$(date -u +%Y%m%dT%H%M%SZ)"
cp -p "$ENV_FILE" "$backup"
if grep -q '^AWG_ENDPOINT_HOST=' "$ENV_FILE"; then
  sed -i "s/^AWG_ENDPOINT_HOST=.*/AWG_ENDPOINT_HOST=$host/" "$ENV_FILE"
else
  printf 'AWG_ENDPOINT_HOST=%s\n' "$host" >>"$ENV_FILE"
fi

wait_healthy() {
  local waited=0 state
  while ((waited < WAIT_SECONDS)); do
    state="$(docker inspect -f '{{.State.Health.Status}}' "$AWG_NAME" 2>/dev/null || echo missing)"
    [[ "$state" == healthy ]] && return 0
    sleep 3
    waited=$((waited + 3))
  done
  return 1
}

restart() {
  (cd "$AWG_DIR" && docker compose -p "$AWG_NAME" up -d) >/dev/null
}

if ! restart || ! wait_healthy; then
  cp -p "$backup" "$ENV_FILE"
  restart || true
  wait_healthy || true
  stop "AmneziaWG did not come back healthy with $host; the previous setting ($current) is back"
fi
say "AWG_ENDPOINT_OK: new configs connect to $host:$port (was $current)"
say "Subscribers need a new config from the bot (/amnezia) to use it."
say "Undo: bash $SRC/scripts/awg_endpoint.sh --set $current"
