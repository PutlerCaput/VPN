#!/usr/bin/env bash
# A second kind of VLESS entry on the DE server: gRPC + REALITY on its own port
# and with its own cover name, next to the existing TCP + REALITY inbound. To a
# traffic filter it looks different, so one rule does not take both down.
#
#   bash vless_transport.sh --check COVER [PORT]   changes nothing: the cover answers
#                                                  TLS 1.3, the port is free, Xray
#                                                  accepts the new config
#   bash vless_transport.sh --apply COVER [PORT]   the same, then switch: Xray config,
#                                                  firewall, users, the bot's setting
#   bash vless_transport.sh --rollback             the config from before --apply
#
# PORT is 2097 unless given. Other kinds and names through the environment:
#   TRANSPORT_NETWORK=tcp TRANSPORT_TAG=VLESS-TCP-REALITY-RU ALLOW_FROM=<RU address> \
#     bash vless_transport.sh --apply <Russian cover> 2098
# makes a TCP entry with a Russian cover that only the RU relay may reach (the RU
# address then forwards a port to it, docs/VPN_ENTRIES.md). Each new entry gets its
# own REALITY key (xray x25519) and short id.
# --apply restarts Marzban once: connected clients drop
# for a few seconds and reconnect by themselves. If Xray does not come back with
# both ports listening, the previous config is put back at once.
# Nothing secret is printed. docs/VPN_ENTRIES.md explains the rest.
set -euo pipefail

CONFIG="${XRAY_CONFIG:-/var/lib/marzban/xray_config.json}"
MARZBAN="${MARZBAN_CONTAINER:-marzban-marzban-1}"
BOT="${BOT_CONTAINER:-node-rental-bot-1}"
TAG="${TRANSPORT_TAG:-VLESS-GRPC-REALITY}"
NETWORK="${TRANSPORT_NETWORK:-grpc}"
ALLOW_FROM="${ALLOW_FROM:-}"   # empty: the port is open to everyone
BASE_PORT="${BASE_PORT:-2096}"
ASSETS="${XRAY_ASSETS:-/var/lib/marzban/assets}"
STATE="${TRANSPORT_STATE:-/var/lib/marzban/.vless-transport-$TAG}"   # path of the backup
WAIT_SECONDS="${WAIT_SECONDS:-60}"
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
SRC="$(dirname "$HERE")"
# Signed releases (docs/SIGNED_RELEASES.md): once the guard is installed, only the
# code the owner signed is built.
RELEASE_GUARD="${RELEASE_GUARD_BIN:-/usr/local/sbin/vpn-release-guard}"
if [[ -x "$RELEASE_GUARD" ]]; then
  "$RELEASE_GUARD" --verify-head "$SRC" || exit 1
fi

say() { printf '%s\n' "$*"; }
stop() {
  printf 'VLESS_TRANSPORT_STOPPED: %s\n' "$*" >&2
  exit 1
}

listening() { ss -lnt 2>/dev/null | awk '{print $4}' | grep -qE "[:.]$1\$"; }

xray_test() {
  docker exec -e "XRAY_LOCATION_ASSET=$ASSETS" "$MARZBAN" \
    xray run -test -config "$1" >/dev/null 2>&1
}

check() {
  local cover="$1" port="$2" candidate="$3" answer key
  [[ -r "$CONFIG" ]] || stop "cannot read $CONFIG"
  [[ "$port" =~ ^[0-9]{4,5}$ ]] || stop "the port is a number, 1024-65535"
  docker inspect "$MARZBAN" >/dev/null 2>&1 || stop "no container $MARZBAN"
  if listening "$port"; then stop "port $port is already in use on this server"; fi
  # REALITY needs a cover that speaks TLS 1.3 and HTTP/2 to this server. curl asks
  # for exactly that and fails otherwise ("xray tls ping" does not print the version).
  answer="$(curl -s -o /dev/null --max-time 10 --tlsv1.3 --http2 \
    -w '%{http_version}' "https://$cover/" 2>/dev/null || true)"
  if [[ "$answer" != 2 ]]; then
    stop "$cover does not answer TLS 1.3 with HTTP/2 from this server (got '${answer:-nothing}'): pick another cover name"
  fi
  # REALITY borrows the cover's key exchange: it must agree on X25519.
  if ! openssl s_client -connect "$cover:443" -servername "$cover" -tls1_3 \
    -groups X25519 </dev/null 2>/dev/null | grep -q X25519; then
    stop "$cover does not agree on X25519 key exchange: pick another cover name"
  fi
  say "cover $cover: TLS 1.3, HTTP/2 and X25519 answer from this server"
  [[ "$NETWORK" =~ ^(grpc|tcp|xhttp)$ ]] || stop "TRANSPORT_NETWORK is grpc, tcp or xhttp"
  # Its own REALITY key, made by Xray itself; it goes to the writer through the
  # environment, never on a command line, and is never printed.
  key="$(docker exec "$MARZBAN" xray x25519 2>/dev/null \
    | sed -nE 's/^ *Private ?[Kk]ey: *([A-Za-z0-9_-]{43}) *$/\1/p' | head -1)"
  [[ -n "$key" ]] || stop "xray x25519 gave no key"
  REALITY_PRIVATE_KEY="$key" python3 "$HERE/xray_add_inbound.py" "$CONFIG" "$candidate" \
    --cover "$cover" --port "$port" --tag "$TAG" --network "$NETWORK" \
    || stop "the candidate was not written"
  unset key
  xray_test "$candidate" || stop "Xray refuses the candidate config; nothing was changed"
  say "Xray accepts the candidate config"
}

# The bot's MARZBAN_EXTRA_INBOUND_TAGS now, and the list with or without one tag.
extra_tags() { docker exec "$BOT" printenv MARZBAN_EXTRA_INBOUND_TAGS 2>/dev/null || true; }
with_tag() {
  local list="$1" tag="$2"
  [[ ",$list," == *",$tag,"* ]] && { printf '%s' "$list"; return; }
  printf '%s' "${list:+$list,}$tag"
}
without_tag() {
  local item out=""
  IFS=, read -ra items <<<"$1"
  for item in "${items[@]}"; do
    [[ -n "$item" && "$item" != "$2" ]] && out="${out:+$out,}$item"
  done
  printf '%s' "$out"
}

restart_and_wait() {
  local port="$1" waited=0
  docker restart "$MARZBAN" >/dev/null
  while ((waited < WAIT_SECONDS)); do
    if listening "$BASE_PORT" && { [[ -z "$port" ]] || listening "$port"; }; then
      return 0
    fi
    sleep 2
    waited=$((waited + 2))
  done
  return 1
}

case "${1:-}" in
  --check)
    cover="${2:-}"
    [[ -n "$cover" ]] || stop "usage: --check COVER [PORT]"
    port="${3:-2097}"
    candidate="$(dirname "$CONFIG")/xray_config.candidate-check.json"
    check "$cover" "$port" "$candidate"
    rm -f "$candidate"
    say "VLESS_TRANSPORT_CHECK_OK: nothing changed. Switch with: --apply $cover $port"
    ;;
  --apply)
    cover="${2:-}"
    [[ -n "$cover" ]] || stop "usage: --apply COVER [PORT]"
    port="${3:-2097}"
    candidate="$(dirname "$CONFIG")/xray_config.candidate.json"
    check "$cover" "$port" "$candidate"
    backup="$CONFIG.before-transport-$(date -u +%Y%m%dT%H%M%SZ)"
    cp -p "$CONFIG" "$backup"
    printf '%s\n%s\n' "$backup" "$port" >"$STATE"
    cp "$candidate" "$CONFIG"
    rm -f "$candidate"
    if ! restart_and_wait "$port"; then
      cp -p "$backup" "$CONFIG"
      restart_and_wait "" || true
      stop "Xray did not come back with ports $BASE_PORT and $port; the previous config is back"
    fi
    say "Xray: $TAG listens on $port, the old entry on $BASE_PORT"
    if command -v ufw >/dev/null 2>&1; then
      if [[ -n "$ALLOW_FROM" ]]; then
        ufw allow from "$ALLOW_FROM" to any port "$port" proto tcp comment "VLESS $TAG" >/dev/null
        say "firewall: $port/tcp open for $ALLOW_FROM only"
      else
        ufw allow "$port/tcp" comment "VLESS $TAG" >/dev/null
        say "firewall: $port/tcp open"
      fi
    fi
    docker exec -i "$BOT" python3 - --attach "$TAG" <"$HERE/vpn_entries.py" \
      || stop "Xray is switched, but the users were not given the entry; run again: docker exec -i $BOT python3 - --attach $TAG < $HERE/vpn_entries.py"
    tags="$(with_tag "$(extra_tags)" "$TAG")"
    bash "$HERE/update_bot.sh" --set "MARZBAN_EXTRA_INBOUND_TAGS=$tags" >/dev/null \
      || stop "the users have the entry, but new users will not: update_bot.sh --set MARZBAN_EXTRA_INBOUND_TAGS=$tags"
    say "new users get it too (MARZBAN_EXTRA_INBOUND_TAGS=$tags)"
    say "VLESS_TRANSPORT_OK: $TAG ($NETWORK) on $port, cover $cover. Clients see it after a subscription update."
    if [[ "$TAG" == VLESS-GRPC-REALITY ]]; then
      say "Undo: bash $SRC/scripts/vless_transport.sh --rollback"
    else
      say "Undo: TRANSPORT_TAG=$TAG${ALLOW_FROM:+ ALLOW_FROM=$ALLOW_FROM} bash $SRC/scripts/vless_transport.sh --rollback"
    fi
    ;;
  --rollback)
    [[ -r "$STATE" ]] || stop "no switch to undo ($STATE is missing)"
    backup="$(sed -n 1p "$STATE")"
    port="$(sed -n 2p "$STATE")"
    [[ -r "$backup" ]] || stop "the backup $backup is gone"
    cp -p "$backup" "$CONFIG"
    restart_and_wait "" || stop "Xray did not come back after the rollback: look at docker logs $MARZBAN"
    if command -v ufw >/dev/null 2>&1 && [[ -n "$port" ]]; then
      ufw delete allow "$port/tcp" >/dev/null 2>&1 || true
      if [[ -n "$ALLOW_FROM" ]]; then
        ufw delete allow from "$ALLOW_FROM" to any port "$port" proto tcp >/dev/null 2>&1 || true
      fi
    fi
    bash "$HERE/update_bot.sh" --set "MARZBAN_EXTRA_INBOUND_TAGS=$(without_tag "$(extra_tags)" "$TAG")" >/dev/null \
      || say "WARN: set it by hand, or new users get a tag Xray no longer has: update_bot.sh --set MARZBAN_EXTRA_INBOUND_TAGS="
    rm -f "$STATE"
    say "VLESS_TRANSPORT_ROLLED_BACK: the config from before --apply is back. Users keep a tag Xray no longer has; Marzban ignores it."
    ;;
  *)
    stop "usage: --check COVER [PORT] | --apply COVER [PORT] | --rollback"
    ;;
esac
