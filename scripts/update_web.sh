#!/usr/bin/env bash
# Safe update of the website and Mini App container on the DE server.
#
#   bash update_web.sh /root/vpn-src            # build and check only, live site unchanged
#   bash update_web.sh /root/vpn-src --apply    # switch, roll back if the checks fail
#   bash update_web.sh --rollback node-rental-web:rollback-<stamp>
#
# What it does, in order:
#   1. finds the running web container (Compose project node-rental-web-local)
#      and keeps its image as node-rental-web:rollback-<stamp>;
#   2. builds the new image from the given source folder; the bot's @name is
#      taken from the running bot (the Mini App also asks the server for it);
#   3. checks that the site and the Mini App are inside the image;
#   4. with --apply: recreates only the web container with the same settings
#      (ACME_EMAIL is read from the running container), waits for "healthy",
#      runs deploy/web/check-web.sh and restores the old image if anything fails.
# It never touches the bot, Marzban, AmneziaWG, the host Caddy, secrets or the database.

set -euo pipefail

PROJECT="node-rental-web-local"
SERVICE="app-web"
BOT_CONTAINER="${BOT_CONTAINER:-node-rental-bot-1}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
LOCAL_ORIGIN="http://127.0.0.1:18082"
SCRIPT_ROOT="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"

say() { printf '%s\n' "$*"; }
fail() { printf 'WEB_UPDATE_FAILED: %s\n' "$*" >&2; exit 1; }

container="$(docker ps -a \
  --filter "label=com.docker.compose.project=$PROJECT" \
  --filter "label=com.docker.compose.service=$SERVICE" \
  --format '{{.Names}}' | head -n1)"
[[ -n "$container" ]] || fail "web container of Compose project $PROJECT not found"

container_env() {
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$1" 2>/dev/null \
    | sed -n "s/^$2=//p" | head -n1
}

acme_email="$(container_env "$container" ACME_EMAIL)"
[[ -n "$acme_email" ]] || fail "ACME_EMAIL not found in $container"

compose_up() {  # <compose file> <image>
  WEB_IMAGE="$2" ACME_EMAIL="$acme_email" \
    docker compose -p "$PROJECT" -f "$1" up -d --no-build --force-recreate "$SERVICE"
}

wait_healthy() {
  local status
  for _ in $(seq 1 45); do
    status="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "$container" 2>/dev/null || true)"
    [[ "$status" == healthy ]] && return 0
    sleep 2
  done
  return 1
}

if [[ "${1:-}" == "--rollback" ]]; then
  [[ -n "${2:-}" ]] || fail "usage: --rollback node-rental-web:rollback-<stamp>"
  docker image inspect "$2" >/dev/null 2>&1 || fail "image $2 not found"
  compose_up "$SCRIPT_ROOT/deploy/web/compose.yaml" "$2"
  wait_healthy && bash "$SCRIPT_ROOT/deploy/web/check-web.sh" "$LOCAL_ORIGIN" \
    && say "WEB_ROLLBACK_DONE $2" || fail "rolled back to $2, but the checks fail"
  exit 0
fi

src="${1:-}"
mode="${2:-}"
[[ -n "$src" && -f "$src/Dockerfile" && -f "$src/deploy/web/compose.yaml" ]] \
  || fail "usage: update_web.sh <source folder> [--apply]"
src="$(cd "$src" && pwd)"
[[ -z "$mode" || "$mode" == --apply ]] || fail "unknown option $mode"

current="$(docker inspect -f '{{.Config.Image}}' "$container")"
rollback="node-rental-web:rollback-$STAMP"
new="node-rental-web:release-$STAMP"
bot_name="$(container_env "$BOT_CONTAINER" BOT_USERNAME)"
[[ "$bot_name" =~ ^[A-Za-z0-9_]{5,32}$ ]] || bot_name=""
# Link previews and the offer pages carry the site's absolute address.
site="$(container_env "$BOT_CONTAINER" PUBLIC_BASE_URL)"
site="${site%/}"
[[ "$site" =~ ^https://[a-z0-9-]+(\.[a-z0-9-]+)+$ ]] || site="https://app.cyberdefency.com"

# Signed releases (docs/SIGNED_RELEASES.md): once the guard is installed, only the
# code the owner signed is built.
RELEASE_GUARD="${RELEASE_GUARD_BIN:-/usr/local/sbin/vpn-release-guard}"
if [[ -x "$RELEASE_GUARD" ]]; then
  "$RELEASE_GUARD" --verify-head "$src" || exit 1
fi
say "SOURCE $src $(git -C "$src" log -1 --format='%h %s' 2>/dev/null || true)"
docker tag "$current" "$rollback"
say "ROLLBACK_IMAGE $rollback (was $current)"

# The image carries a digest of what it is built from, so the monthly
# auto-update can tell whether the checkout still holds the code that runs.
digest="$(bash "$SCRIPT_ROOT/scripts/source_digest.sh" "$src" 2>/dev/null || true)"
docker build --target web --label "node.rental.source=$digest" \
  --build-arg "VITE_BOT_USERNAME=$bot_name" \
  --build-arg "PUBLIC_ORIGIN=$site" -t "$new" "$src" \
  || fail "build failed, live site unchanged"
docker run --rm --entrypoint sh "$new" -c \
  'test -s /srv/pwa/index.html && test -s /srv/tma/index.html && ls /srv/tma/assets/*.js >/dev/null' \
  || fail "site files missing in $new, live site unchanged"
say "BUILD_OK $new (bot name in build: ${bot_name:-none, taken from the server at runtime}; site: $site)"

if [[ "$mode" != --apply ]]; then
  say "CHECK_OK: live site unchanged. To switch, run again with --apply."
  exit 0
fi

compose_up "$src/deploy/web/compose.yaml" "$new"
if wait_healthy && bash "$src/deploy/web/check-web.sh" "$LOCAL_ORIGIN"; then
  say "WEB_UPDATE_OK $current -> $new"
  say "Rollback if needed: bash $src/scripts/update_web.sh --rollback $rollback"
else
  say "Checks failed, restoring $rollback"
  compose_up "$src/deploy/web/compose.yaml" "$rollback"
  wait_healthy || true
  fail "restored $rollback"
fi
