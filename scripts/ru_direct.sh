#!/usr/bin/env bash
# Russian sites past the server, handed to the apps inside the subscription.
#
#   bash scripts/ru_direct.sh            # what is set now; changes nothing
#   bash scripts/ru_direct.sh --apply    # turn it on
#   bash scripts/ru_direct.sh --remove   # turn it off
#
# Traffic that has reached the server leaves it with the server's foreign
# address, so only the app on the device can open Russian sites directly.
# Hiddify Manager hands the rule to the app with the subscription: v2rayNG then receives
# full Xray configs (USE_CUSTOM_JSON_FOR_V2RAYNG), the sing-box apps (Karing
# among them) a sing-box config, both built from templates that
# ru_direct_templates.py makes from Hiddify Manager's own defaults. Apps that receive
# only server links (NekoBox, OneXray, v2rayN) keep their steps in the bot.
#
# --apply checks the new config with the Xray core in Hiddify Manager's container
# ("Configuration OK") before anything changes, keeps a copy of Hiddify Manager's .env,
# restarts Hiddify Manager and puts the copy back if the panel does not answer. The bot
# is then told (RU_DIRECT_SUBSCRIPTION=true), so its v2rayNG guide says that
# nothing has to be set up. docs/CONNECT_APPS.md, "Российские сайты напрямую".
set -euo pipefail

HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
# A HIDDIFY_DIR given by the caller is a sandbox (tests), not the server.
if [[ -z "${HIDDIFY_DIR:-}" ]]; then
  [[ $EUID -eq 0 ]] || { printf 'RU_DIRECT_FAILED: run as root\n' >&2; exit 1; }
fi
HIDDIFY_DIR="${HIDDIFY_DIR:-/opt/hiddify-manager}"
HIDDIFY_DATA="${HIDDIFY_DATA:-/opt/hiddify-manager}"
CONTAINER="${HIDDIFY_CONTAINER:-hiddify-manager}"
HIDDIFY_URL="${HIDDIFY_URL:-http://127.0.0.1:8000}"
DEFAULTS="${HIDDIFY_DEFAULT_TEMPLATES:-/code/app/templates}"
BOT_UPDATER="${BOT_UPDATER:-$HERE/update_bot.sh}"
WAIT_SECONDS="${WAIT_SECONDS:-90}"
WAIT_STEP_SECONDS="${WAIT_STEP_SECONDS:-3}"
ENV_FILE="$HIDDIFY_DIR/.env"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
V2RAY="v2ray/ru-direct.json"
SINGBOX="singbox/ru-direct.json"

say() { printf '%s\n' "$*"; }
fail() { printf 'RU_DIRECT_FAILED: %s\n' "$*" >&2; exit 1; }

MODE=report
case "${1:-}" in
  "") ;;
  --apply) MODE=apply ;;
  --remove) MODE=remove ;;
  *) fail "usage: ru_direct.sh [--apply | --remove]" ;;
esac
[[ $# -le 1 ]] || fail "usage: ru_direct.sh [--apply | --remove]"
[[ -f "$ENV_FILE" ]] || fail "$ENV_FILE not found (is Hiddify Manager installed in $HIDDIFY_DIR?)"

# The value Hiddify Manager reads: the last uncommented "KEY = value", quotes removed.
env_value() {
  sed -nE "s/^[[:space:]]*${1}[[:space:]]*=[[:space:]]*[\"']?([^\"']*)[\"']?[[:space:]]*$/\1/p" "$ENV_FILE" | tail -n 1
}

# Every uncommented line for each key is dropped, then KEY="value" is added.
set_env() {
  local tmp="$ENV_FILE.tmp-$STAMP" pair key
  cp -p "$ENV_FILE" "$tmp"
  for pair in "$@"; do
    key="${pair%%=*}"
    grep -vE "^[[:space:]]*${key}[[:space:]]*=" "$tmp" >"$tmp.next" || true
    mv "$tmp.next" "$tmp"
    [[ "$pair" == *=* ]] && printf '%s="%s"\n' "$key" "${pair#*=}" >>"$tmp"
  done
  cat "$tmp" >"$ENV_FILE"  # keeps owner and permissions
  rm -f "$tmp"
}

running() { [[ "$(docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null)" == true ]]; }

# Answers at all (a 401 for the admin API counts): the panel is up.
panel_up() {
  local code
  code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$HIDDIFY_URL/api/v2/panel/ping/" 2>/dev/null || true)"
  [[ "$code" =~ ^[1-4][0-9][0-9]$ ]]
}

wait_panel() {
  local waited=0
  while ((waited < WAIT_SECONDS)); do
    running && panel_up && return 0
    sleep "$WAIT_STEP_SECONDS"
    waited=$((waited + WAIT_STEP_SECONDS))
  done
  return 1
}

restart_panel() {
  if command -v hiddify >/dev/null 2>&1; then
    hiddify restart -n >/dev/null 2>&1 || true
  else
    (cd "$HIDDIFY_DIR" && docker compose up -d --force-recreate >/dev/null 2>&1) || true
  fi
}

tell_bot() {
  if bash "$BOT_UPDATER" --set "RU_DIRECT_SUBSCRIPTION=$1" >/dev/null 2>&1; then
    say "  bot: RU_DIRECT_SUBSCRIPTION=$1"
  else
    say "WARN: the bot was not told; run: bash scripts/update_bot.sh --set RU_DIRECT_SUBSCRIPTION=$1"
  fi
}

templates_dir="$(env_value CUSTOM_TEMPLATES_DIRECTORY)"
templates_dir="${templates_dir:-$HIDDIFY_DATA/templates/}"
templates_dir="${templates_dir%/}"
v2ray_now="$(env_value V2RAY_SUBSCRIPTION_TEMPLATE)"
singbox_now="$(env_value SINGBOX_SUBSCRIPTION_TEMPLATE)"
json_now="$(env_value USE_CUSTOM_JSON_FOR_V2RAYNG)"
on=0
if [[ "$v2ray_now" == "$V2RAY" && "$singbox_now" == "$SINGBOX" && "${json_now,,}" == true \
  && -f "$templates_dir/$V2RAY" && -f "$templates_dir/$SINGBOX" ]]; then
  on=1
fi

if [[ "$MODE" == report ]]; then
  say "Hiddify Manager: $HIDDIFY_DIR (container $CONTAINER)"
  say "  templates folder: $templates_dir"
  say "  V2RAY_SUBSCRIPTION_TEMPLATE=${v2ray_now:-default}"
  say "  SINGBOX_SUBSCRIPTION_TEMPLATE=${singbox_now:-default}"
  say "  USE_CUSTOM_JSON_FOR_V2RAYNG=${json_now:-False}"
  if ((on)); then
    say "RU_DIRECT_STATE on: v2rayNG and the sing-box apps get the rules with the subscription"
  else
    say "RU_DIRECT_STATE off: turn on with: bash scripts/ru_direct.sh --apply"
  fi
  exit 0
fi

running || fail "the Hiddify Manager container $CONTAINER is not running"

if [[ "$MODE" == remove ]]; then
  BACKUP="$ENV_FILE.before-ru-direct-$STAMP"
  cp -p "$ENV_FILE" "$BACKUP"
  # Back to Hiddify Manager's defaults; the templates folder setting stays (harmless).
  set_env V2RAY_SUBSCRIPTION_TEMPLATE SINGBOX_SUBSCRIPTION_TEMPLATE USE_CUSTOM_JSON_FOR_V2RAYNG
  restart_panel
  if ! wait_panel; then
    cp -p "$BACKUP" "$ENV_FILE"
    restart_panel
    wait_panel || fail "Hiddify Manager did not answer after the change, and not after going back either: check docker logs $CONTAINER"
    fail "Hiddify Manager did not answer after the change; the previous settings are back"
  fi
  tell_bot false
  say "RU_DIRECT_REMOVED: apps get plain server links again (copy of the old settings: $BACKUP)"
  exit 0
fi

# ------------------------------------------------------------------- apply
# Hiddify Manager reads the templates inside its container. The data folder is mounted
# there at the same path; a templates folder elsewhere would not be seen.
case "$templates_dir/" in
  "${HIDDIFY_DATA%/}"/*) ;;
  *) fail "CUSTOM_TEMPLATES_DIRECTORY ($templates_dir) is outside $HIDDIFY_DATA; set it by hand" ;;
esac
# A template the owner made is not replaced.
for pair in "V2RAY_SUBSCRIPTION_TEMPLATE|$v2ray_now|v2ray/default.json|$V2RAY" \
  "SINGBOX_SUBSCRIPTION_TEMPLATE|$singbox_now|singbox/default.json|$SINGBOX"; do
  IFS='|' read -r key now default ours <<<"$pair"
  if [[ -n "$now" && "$now" != "$default" && "$now" != "$ours" ]]; then
    fail "$key is set to your own template ($now); add the rules to it by hand"
  fi
done

xray_bin="$(env_value XRAY_EXECUTABLE_PATH)"
xray_bin="${xray_bin:-/usr/local/bin/xray}"
assets="$(env_value XRAY_ASSETS_PATH)"
assets="${assets:-/usr/local/share/xray}"

mkdir -p "$templates_dir/v2ray" "$templates_dir/singbox"
work="$(mktemp -d)"
trap 'rm -rf "$work"; rm -f "$templates_dir/.ru-direct-check.json"' EXIT
docker exec "$CONTAINER" cat "$DEFAULTS/v2ray/default.json" >"$work/v2ray-default.json" \
  || fail "cannot read Hiddify Manager's default v2ray template ($DEFAULTS/v2ray/default.json)"
docker exec "$CONTAINER" cat "$DEFAULTS/singbox/default.json" >"$work/singbox-default.json" \
  || fail "cannot read Hiddify Manager's default sing-box template ($DEFAULTS/singbox/default.json)"
python3 "$HERE/ru_direct_templates.py" v2ray <"$work/v2ray-default.json" >"$work/v2ray.json" \
  || fail "the v2ray template could not be made"
python3 "$HERE/ru_direct_templates.py" singbox <"$work/singbox-default.json" >"$work/singbox.json" \
  || fail "the sing-box template could not be made"
python3 "$HERE/ru_direct_templates.py" check <"$work/v2ray.json" >"$templates_dir/.ru-direct-check.json" \
  || fail "the check config could not be made"
docker exec "$CONTAINER" test -f "$templates_dir/.ru-direct-check.json" \
  || fail "Hiddify Manager's container does not see $templates_dir; nothing was changed"

# The Xray core of the panel reads the config the way the app's core will.
check="$(docker exec -e XRAY_LOCATION_ASSET="$assets" "$CONTAINER" "$xray_bin" run -test \
  -c "$templates_dir/.ru-direct-check.json" 2>&1 || true)"
if ! grep -q "Configuration OK" <<<"$check"; then
  printf '%s\n' "$check" | tail -n 5 >&2
  fail "Xray rejected the config; nothing was changed"
fi
say "  xray: Configuration OK"

install -m 644 "$work/v2ray.json" "$templates_dir/$V2RAY"
install -m 644 "$work/singbox.json" "$templates_dir/$SINGBOX"
if ((on)); then
  # Hiddify Manager reads a template file anew when it changes: no restart needed.
  tell_bot true
  say "RU_DIRECT_OK: already on; templates refreshed from Hiddify Manager's current defaults"
  exit 0
fi

# Hiddify Manager drops the connections for the few seconds of its restart.
BACKUP="$ENV_FILE.before-ru-direct-$STAMP"
cp -p "$ENV_FILE" "$BACKUP"
set_env "CUSTOM_TEMPLATES_DIRECTORY=$templates_dir/" "V2RAY_SUBSCRIPTION_TEMPLATE=$V2RAY" \
  "SINGBOX_SUBSCRIPTION_TEMPLATE=$SINGBOX" "USE_CUSTOM_JSON_FOR_V2RAYNG=True"
restart_panel
if wait_panel && [[ "$(docker exec "$CONTAINER" printenv V2RAY_SUBSCRIPTION_TEMPLATE 2>/dev/null)" == "$V2RAY" ]]; then
  tell_bot true
  say "RU_DIRECT_OK: v2rayNG and the sing-box apps get the rules with the next subscription update"
  say "  copy of the old settings: $BACKUP"
  say "  check on a phone with v2rayNG: ⋮ → Update subscription, connect, then"
  say "  yandex.ru/internet shows your own address and ifconfig.me shows the server's"
  exit 0
fi
cp -p "$BACKUP" "$ENV_FILE"
restart_panel
wait_panel || fail "Hiddify Manager did not take the change, and did not answer after going back either: check docker logs $CONTAINER"
fail "Hiddify Manager did not take the change; the previous settings are back (nothing changed for the apps)"
