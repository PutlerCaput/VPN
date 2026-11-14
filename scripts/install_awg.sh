#!/usr/bin/env bash
# Install the subscriber AmneziaWG instance next to the bot (DE server).
#
#   bash install_awg.sh <new source folder>            # check only, changes nothing
#   bash install_awg.sh <new source folder> --apply    # install and connect the bot
#   bash install_awg.sh --remove                       # stop it (keys are kept)
#
# Environment overrides: AWG_PORT (39051), AWG_SUBNET (10.67.0.0/24),
# AWG_ENDPOINT_HOST (default: this server's public IPv4), CONTAINER (bot).
#
# It creates its own container "cyberdefency-awg" with its own UDP port,
# subnet and keys. Existing Amnezia containers (for example amnezia-awg2)
# are listed and left exactly as they are.

set -euo pipefail

CONTAINER="${CONTAINER:-node-rental-bot-1}"
AWG_DIR="${AWG_DIR:-/root/cyberdefency-awg}"
AWG_PORT="${AWG_PORT:-39051}"
AWG_SUBNET="${AWG_SUBNET:-10.67.0.0/24}"
AWG_NAME="cyberdefency-awg"
AGENT_URL="http://$AWG_NAME:8090"

say() { printf '%s\n' "$*"; }
fail() { printf 'AWG_INSTALL_FAILED: %s\n' "$*" >&2; exit 1; }

if [[ "${1:-}" == "--remove" ]]; then
  [[ -f "$AWG_DIR/compose.yaml" ]] || fail "$AWG_DIR not found"
  (cd "$AWG_DIR" && docker compose -p "$AWG_NAME" down)
  say "AWG_REMOVED (state volume with keys kept). Turn it off in the bot:"
  say "  bash <source>/scripts/update_bot.sh --set AWG_ENABLED=false"
  exit 0
fi

SOURCE="${1:-}"
[[ -n "$SOURCE" && -f "$SOURCE/deploy/awg/Dockerfile" ]] \
  || fail "usage: install_awg.sh <new source folder> [--apply]"
# Signed releases (docs/SIGNED_RELEASES.md): once the guard is installed, only the
# code the owner signed is built.
RELEASE_GUARD="${RELEASE_GUARD_BIN:-/usr/local/sbin/vpn-release-guard}"
if [[ -x "$RELEASE_GUARD" ]]; then
  "$RELEASE_GUARD" --verify-head "$SOURCE" || exit 1
fi
APPLY=0
[[ "${2:-}" == "--apply" ]] && APPLY=1

[[ "$AWG_PORT" =~ ^[0-9]+$ ]] && ((AWG_PORT >= 1024 && AWG_PORT <= 65535)) \
  || fail "AWG_PORT must be 1024-65535"
[[ "$AWG_SUBNET" =~ ^10\.[0-9]+\.[0-9]+\.0/24$ ]] || fail "AWG_SUBNET must look like 10.x.y.0/24"

docker inspect "$CONTAINER" >/dev/null 2>&1 || fail "bot container $CONTAINER not found"
NETWORKS="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}' "$CONTAINER")"
BOT_NETWORK="${NETWORKS%% *}"
[[ -n "$BOT_NETWORK" && "$BOT_NETWORK" != host && "$BOT_NETWORK" != none ]] \
  || fail "bot network '$BOT_NETWORK' is not a Docker bridge network; set up by hand"

if [[ -z "${AWG_ENDPOINT_HOST:-}" ]]; then
  AWG_ENDPOINT_HOST="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1;i<NF;i++) if ($i=="src") print $(i+1)}')"
fi
[[ "$AWG_ENDPOINT_HOST" =~ ^[A-Za-z0-9.-]+$ ]] || fail "set AWG_ENDPOINT_HOST to the public IP or name"
case "$AWG_ENDPOINT_HOST" in
  10.*|192.168.*|172.1[6-9].*|172.2[0-9].*|172.3[0-1].*|127.*)
    fail "$AWG_ENDPOINT_HOST is private; set AWG_ENDPOINT_HOST to the public IP" ;;
esac

if ss -Hlun 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$AWG_PORT\$"; then
  if ! docker ps --format '{{.Names}}' | grep -qx "$AWG_NAME"; then
    fail "UDP port $AWG_PORT is already in use; choose another AWG_PORT"
  fi
fi

say "bot container:   $CONTAINER (network $BOT_NETWORK)"
say "new instance:    $AWG_NAME, UDP $AWG_PORT, subnet $AWG_SUBNET"
say "client endpoint: $AWG_ENDPOINT_HOST:$AWG_PORT"
OTHERS="$(docker ps -a --format '{{.Names}}' | grep -i amnezia || true)"
if [[ -n "$OTHERS" ]]; then
  say "existing Amnezia containers (not changed): $(printf '%s ' $OTHERS)"
fi

if ((APPLY == 0)); then
  say "AWG_CHECK_OK: nothing changed. Run again with --apply to install."
  exit 0
fi

install -d -m 700 "$AWG_DIR" "$AWG_DIR/secrets"
cp "$SOURCE/deploy/awg/Dockerfile" "$SOURCE/deploy/awg/agent.py" "$SOURCE/deploy/awg/compose.yaml" "$AWG_DIR/"
TOKEN_FILE="$AWG_DIR/secrets/awg_agent_token.txt"
if [[ ! -s "$TOKEN_FILE" ]]; then
  (umask 077 && head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' >"$TOKEN_FILE")
fi
chmod 600 "$TOKEN_FILE"
cat >"$AWG_DIR/.env" <<ENV
AWG_PORT=$AWG_PORT
AWG_SUBNET=$AWG_SUBNET
AWG_ENDPOINT_HOST=$AWG_ENDPOINT_HOST
BOT_NETWORK=$BOT_NETWORK
ENV
chmod 600 "$AWG_DIR/.env"

(cd "$AWG_DIR" && docker compose -p "$AWG_NAME" up -d --build) || fail "start failed"
for _ in $(seq 1 40); do
  state="$(docker inspect -f '{{.State.Health.Status}}' "$AWG_NAME" 2>/dev/null || echo missing)"
  [[ "$state" == healthy ]] && break
  sleep 3
done
[[ "$state" == healthy ]] || { docker logs --tail 20 "$AWG_NAME" >&2; fail "$AWG_NAME is not healthy"; }
say "AWG_RUNNING $AWG_NAME"

docker exec "$CONTAINER" python3 -c \
  "import urllib.request; urllib.request.urlopen('$AGENT_URL/healthz', timeout=5)" \
  || fail "the bot cannot reach $AGENT_URL"
say "BOT_CAN_REACH_AGENT"

bash "$SOURCE/scripts/update_bot.sh" --set AWG_ENABLED=true "AWG_AGENT_URL=$AGENT_URL" \
  "AWG_AGENT_TOKEN=$(cat "$TOKEN_FILE")" || fail "bot settings not changed"
say "AWG_INSTALL_OK: subscribers can add AmneziaWG devices with /amnezia"
say "If the provider has its own firewall, allow UDP $AWG_PORT there."
