#!/usr/bin/env bash
# Safe update of the bot container on the DE server.
#
#   bash update_bot.sh /root/vpn-update            # check only, changes nothing live
#   bash update_bot.sh /root/vpn-update --apply    # switch, roll back if unhealthy
#   bash update_bot.sh --rollback node-rental-bot:rollback-<stamp> [.env backup]
#   bash update_bot.sh --set-tiers standard        # switch the tariffs on sale
#   bash update_bot.sh --set CONFIG_DELIVERY=subscription STARS_AUTORENEW_ENABLED=true
#
# Optional with --apply: --env-add appends the new settings that are missing
# from .env (STARS_ENABLED, STARS_RUB_PER_STAR, YOOKASSA_IN_BOT,
# TRIAL_TRAFFIC_GB, ENABLED_TIERS). Existing lines are never changed. .env is
# copied first and restored on rollback: the previous release does not know
# the "standard" tier and would refuse to start with the new settings.
#
# What it does, in order:
#   1. finds the running bot container and the image reference it uses;
#   2. keeps that image as node-rental-bot:rollback-<stamp>;
#   3. builds the new image from the given source folder;
#   4. writes a checked SQLCipher backup of the live database;
#   5. applies the new schema to a COPY of the database (db_tool migrate-check);
#   6. with --apply: points the same image reference at the new build,
#      recreates only the bot service and waits for "healthy"; otherwise
#      restores the rollback image automatically.
# It never touches Marzban, AmneziaWG, Caddy, secrets or the database key.

set -euo pipefail

CONTAINER="${CONTAINER:-node-rental-bot-1}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
SELF="$(readlink -f "$0")"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-150}"

say() { printf '%s\n' "$*"; }
fail() { printf 'UPDATE_FAILED: %s\n' "$*" >&2; exit 1; }

label() {
  docker inspect -f "{{ index .Config.Labels \"$1\" }}" "$CONTAINER" 2>/dev/null || true
}

wait_healthy() {
  local deadline=$((SECONDS + HEALTH_TIMEOUT)) state
  while ((SECONDS < deadline)); do
    state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER" 2>/dev/null || echo missing)"
    case "$state" in
      healthy) return 0 ;;
      none) fail "container has no healthcheck; check it manually" ;;
    esac
    sleep 5
  done
  return 1
}

recreate() {
  (cd "$WORKDIR" && docker compose -p "$PROJECT" up -d --no-deps --no-build --force-recreate "$SERVICE")
}

docker inspect "$CONTAINER" >/dev/null 2>&1 || fail "container $CONTAINER not found"
IMAGE_REF="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER")"
PROJECT="$(label com.docker.compose.project)"
SERVICE="$(label com.docker.compose.service)"
WORKDIR="$(label com.docker.compose.project.working_dir)"
[[ -n "$PROJECT" && -n "$SERVICE" && -d "$WORKDIR" ]] || fail "not a Compose container"

if [[ "${1:-}" == "--rollback" ]]; then
  [[ -n "${2:-}" ]] || fail "usage: --rollback node-rental-bot:rollback-<stamp> [env backup]"
  docker image inspect "$2" >/dev/null 2>&1 || fail "image $2 not found"
  if [[ -n "${3:-}" ]]; then
    [[ -f "$3" ]] || fail "$3 not found"
    cp -p "$3" "$WORKDIR/.env"
    say "ENV_RESTORED from $3"
  fi
  docker tag "$2" "$IMAGE_REF"
  recreate
  wait_healthy && say "ROLLBACK_DONE $IMAGE_REF <- $2" || fail "rolled back, but not healthy"
  exit 0
fi

# Settings an owner may change with --set; anything else is edited by hand.
SETTABLE_KEYS="ENABLED_TIERS CONFIG_DELIVERY SUBSCRIPTION_BASE_URL MAIN_LOCATION_NAME \
STARS_ENABLED STARS_AUTORENEW_ENABLED STARS_RUB_PER_STAR YOOKASSA_IN_BOT TRIAL_TRAFFIC_GB"

if [[ "${1:-}" == "--set-tiers" ]]; then
  set -- --set "ENABLED_TIERS=${2:-}"
fi
if [[ "${1:-}" == "--set" ]]; then
  shift
  (($# > 0)) || fail "usage: --set KEY=VALUE [KEY=VALUE ...]  keys: $SETTABLE_KEYS"
  for pair in "$@"; do
    key="${pair%%=*}"
    value="${pair#*=}"
    [[ "$pair" == *=* && " $SETTABLE_KEYS " == *" $key "* ]] \
      || fail "not allowed: $key (allowed: $SETTABLE_KEYS)"
    [[ -n "$value" && "$value" != *$'\n'* && "$value" != *[\"\'\\\$\`]* ]] \
      || fail "invalid value for $key"
    if [[ "$key" == ENABLED_TIERS ]]; then
      [[ "$value" =~ ^(standard|basic|complete|prime)(,(standard|basic|complete|prime))*$ ]] \
        || fail "usage: --set ENABLED_TIERS=standard  (standard, basic, complete, prime)"
    fi
  done
  ENV_FILE="$WORKDIR/.env"
  [[ -f "$ENV_FILE" ]] || fail "$ENV_FILE not found"
  ENV_BACKUP="$ENV_FILE.before-set-$STAMP"
  cp -p "$ENV_FILE" "$ENV_BACKUP"
  for pair in "$@"; do
    key="${pair%%=*}"
    grep -v "^$key=" "$ENV_FILE" >"$ENV_FILE.tmp-$STAMP" || true
    printf '%s\n' "$pair" >>"$ENV_FILE.tmp-$STAMP"
    cat "$ENV_FILE.tmp-$STAMP" >"$ENV_FILE"  # keeps owner and permissions
    rm -f "$ENV_FILE.tmp-$STAMP"
  done
  say "ENV_BACKUP $ENV_BACKUP"
  recreate
  if wait_healthy; then
    say "SET_OK $*"
    exit 0
  fi
  cp -p "$ENV_BACKUP" "$ENV_FILE"
  recreate
  wait_healthy && fail "bot refused the new settings; previous .env restored (healthy)" \
    || fail "previous .env restored, but the bot is not healthy"
fi

SOURCE="${1:-}"
[[ -n "$SOURCE" && -f "$SOURCE/Dockerfile" && -f "$SOURCE/main.py" ]] \
  || fail "usage: update_bot.sh <new source folder> [--apply] [--env-add]"
shift
APPLY=0
ENV_ADD=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --env-add) ENV_ADD=1 ;;
    *) fail "unknown option $arg" ;;
  esac
done
((ENV_ADD == 0 || APPLY == 1)) || fail "--env-add is only allowed together with --apply"

DATA_DIR="$WORKDIR/data"
KEY_FILE="$WORKDIR/secrets/db_key.txt"
ENV_FILE="$WORKDIR/.env"
[[ -s "$DATA_DIR/bot.db" ]] || fail "$DATA_DIR/bot.db not found or empty"
[[ -f "$KEY_FILE" ]] || fail "$KEY_FILE not found (the database key is required)"
[[ -f "$ENV_FILE" ]] || fail "$ENV_FILE not found"

say "container=$CONTAINER service=$SERVICE project=$PROJECT"
say "workdir=$WORKDIR image=$IMAGE_REF"

ROLLBACK="node-rental-bot:rollback-$STAMP"
docker tag "$(docker inspect -f '{{.Image}}' "$CONTAINER")" "$ROLLBACK"
say "ROLLBACK_IMAGE_SAVED $ROLLBACK"

NEW="node-rental-bot:update-$STAMP"
docker build --target bot -t "$NEW" "$SOURCE" || fail "build failed; nothing was changed"
say "BUILD_OK $NEW"

# The new image's tool works whatever the old image contains. SQLite's online
# backup is safe while the live bot keeps writing.
run_tool() {
  docker run --rm --user 1000:1000 --read-only --tmpfs /tmp --network none \
    -v "$DATA_DIR:/app/data" -v "$KEY_FILE:/run/secrets/db_key:ro" \
    "$NEW" python scripts/db_tool.py "$@"
}
BACKUP="before-update-$STAMP.db"
run_tool backup /app/data/bot.db "/app/data/$BACKUP" \
  || fail "database backup failed; nothing was changed"
say "DB_BACKUP_OK $DATA_DIR/$BACKUP"

run_tool migrate-check /app/data/bot.db \
  || fail "migration check failed on the copy; nothing was changed"

if ((APPLY == 0)); then
  say "CHECK_OK: live bot unchanged. To switch, run again with --apply."
  exit 0
fi

ENV_BACKUP="$ENV_FILE.before-update-$STAMP"
cp -p "$ENV_FILE" "$ENV_BACKUP"
say "ENV_BACKUP $ENV_BACKUP"
if ((ENV_ADD)); then
  add_env() {
    if ! grep -q "^$1=" "$ENV_FILE"; then
      printf '%s=%s\n' "$1" "$2" >>"$ENV_FILE"
      say "ENV_ADDED $1=$2"
    else
      say "ENV_KEPT $1 (already set)"
    fi
  }
  add_env STARS_ENABLED true
  add_env STARS_RUB_PER_STAR 1.60
  add_env YOOKASSA_IN_BOT false
  add_env TRIAL_TRAFFIC_GB 3
  add_env ENABLED_TIERS standard
fi

docker tag "$NEW" "$IMAGE_REF"
recreate
if wait_healthy; then
  say "UPDATE_OK $IMAGE_REF -> $NEW"
  say "Rollback if needed: bash $SELF --rollback $ROLLBACK $ENV_BACKUP"
  exit 0
fi

say "New bot is not healthy; restoring $ROLLBACK and .env"
docker logs --tail 30 "$CONTAINER" 2>&1 | grep -viE 'token|secret|key' || true
cp -p "$ENV_BACKUP" "$ENV_FILE"
docker tag "$ROLLBACK" "$IMAGE_REF"
recreate
wait_healthy && fail "rolled back to the previous image (healthy)" \
  || fail "rolled back, but the previous image is not healthy either"
