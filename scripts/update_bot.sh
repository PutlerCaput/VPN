#!/usr/bin/env bash
# Safe update of the bot container on the DE server.
#
#   bash update_bot.sh /root/vpn-update            # check only, changes nothing live
#   bash update_bot.sh /root/vpn-update --apply    # switch, roll back if unhealthy
#   bash update_bot.sh --rollback node-rental-bot:rollback-<stamp> [.env backup]
#   bash update_bot.sh --set-tiers standard        # switch the tariffs on sale
#   bash update_bot.sh --set CONFIG_DELIVERY=subscription STARS_AUTORENEW_ENABLED=true
#   bash update_bot.sh --set-secret TELEGRAM_PAYMENT_TOKEN   # typed hidden on the server
#   bash update_bot.sh --payment-status                       # what can take payments now
#   bash update_bot.sh --payment-qr-check   # can YooKassa give SBP payments by QR?
#   bash update_bot.sh --set YOOKASSA_CONFIRMATION=qr   # the same check, then switch
#   bash update_bot.sh --doctor                               # read-only check of everything
#   bash update_bot.sh --disable-usdt                         # turn the /usdt payments off
#   bash update_bot.sh --set-public-url https://app.example.com  # site address for links
#   ... | bash update_bot.sh --database-gateway http://10.204.0.1:8444
#                                   # the database on the other server; the token
#                                   # arrives on standard input and goes to
#                                   # secrets/db_gateway_token.txt, never to .env
#   bash update_bot.sh --database-local                       # back to the local file
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
STARS_ENABLED STARS_AUTORENEW_ENABLED STARS_RUB_PER_STAR YOOKASSA_IN_BOT TRIAL_TRAFFIC_GB \
AWG_ENABLED AWG_AGENT_URL AWG_AGENT_TOKEN TELEGRAM_PAYMENT_TOKEN \
YOOKASSA_RECEIPT_MODE ACTIVITY_LOGGING_ENABLED AUTOBAN_MODE TELEGRAM_PROXY_URL \
SERVER_TRAFFIC_LIMIT_GB SERVER_TRAFFIC_COUNTS SERVER_TRAFFIC_RESET_DAY SERVER_TRAFFIC_PAUSE_TRIALS \
RU_DIRECT_SUBSCRIPTION CONN_LOG_RETENTION_DAYS YOOKASSA_CONFIRMATION \
MARZBAN_EXTRA_INBOUND_TAGS SMTP_HOST SMTP_PORT SMTP_SECURITY SMTP_USERNAME SMTP_FROM SMTP_FROM_NAME SMTP_PASSWORD"

# Creates a 10 RUB test payment with the QR confirmation in the live shop and
# says whether YooKassa answers with an SBP link. Prints no secret and no link.
payment_qr_check() {
  docker exec -i "$CONTAINER" python3 - <"$(dirname "$SELF")/payment_qr_check.py"
}

if [[ "${1:-}" == "--payment-qr-check" ]]; then
  payment_qr_check
  exit $?
fi

if [[ "${1:-}" == "--payment-status" ]]; then
  # Safe summary only: which payment paths are configured, never any value.
  docker exec -i "$CONTAINER" python3 - <<'PY'
import json, sys, urllib.request
sys.path.insert(0, "/app")
from config import get_settings
s = get_settings()
token = s.telegram_payment_token.get_secret_value() if s.telegram_payment_token else ""
rest = s.sbp_provider == "yookassa" and bool(s.yookassa_shop_id and s.yookassa_secret_key)
placeholder = "REPLACE_WITH" in (s.sbp_qr_url + s.sbp_payment_url).upper()
menu = []
if s.stars_enabled:
    menu.append("stars")
# Rubles are SBP only: the Telegram invoice (card form) no longer counts.
if s.yookassa_in_bot and (rest or (s.sbp_provider == "manual" and not placeholder)):
    menu.append("yookassa")
req = urllib.request.Request("http://127.0.0.1:8080/yookassa/webhook", data=b"{}",
                             headers={"Content-Type": "application/json"}, method="POST")
try:
    webhook = urllib.request.urlopen(req, timeout=5).status
except urllib.error.HTTPError as exc:
    webhook = exc.code
except OSError:
    webhook = "unreachable"
print(json.dumps({
    "yookassa_in_telegram_token": bool(token),
    "yookassa_telegram_mode": ("TEST" if ":TEST:" in token.upper() else "LIVE") if token else None,
    "yookassa_rest_keys": rest,
    "sbp_provider": s.sbp_provider,
    "sbp_placeholder_links": placeholder,
    "yookassa_button_in_bot": s.yookassa_in_bot,
    "stars_enabled": s.stars_enabled,
    "bot_payment_menu": menu,
    "local_webhook_status": webhook,
    "receipt_mode": s.yookassa_receipt_mode,
    "sbp_confirmation": getattr(s, "yookassa_confirmation", "redirect"),
}, ensure_ascii=False, indent=1))
PY
  exit $?
fi

if [[ "${1:-}" == "--doctor" ]]; then
  # Read-only: settings, payments, panel, Telegram, site and web container.
  ok=0 warn=0 bad=0
  report() {
    case "$1" in OK) ok=$((ok + 1)) ;; WARN) warn=$((warn + 1)) ;; *) bad=$((bad + 1)) ;; esac
    printf '%-4s %s\n' "$1" "$2"
  }
  state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$CONTAINER")"
  [[ "$state" == healthy ]] && report OK "bot container: healthy" || report FAIL "bot container: $state"
  checks="$(docker exec -i "$CONTAINER" python3 - <"$(dirname "$SELF")/doctor_bot.py" 2>/dev/null)" \
    || report FAIL "checks inside the bot container did not finish"
  while IFS= read -r row; do
    case "$row" in OK\ * | WARN\ * | FAIL\ *) report "${row%% *}" "${row#* }" ;; esac
  done <<<"$checks"

  web="$(docker ps -a --filter label=com.docker.compose.project=node-rental-web-local \
    --filter label=com.docker.compose.service=app-web --format '{{.Names}}' | head -n1)"
  if [[ -z "$web" ]]; then
    report FAIL "web container (site and Mini App) not found"
  else
    web_state="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$web")"
    [[ "$web_state" == healthy ]] && report OK "web container: healthy" || report FAIL "web container: $web_state"
  fi

  site="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$CONTAINER" | sed -n 's/^PUBLIC_BASE_URL=//p' | head -n1)"
  site="${site%/}"
  if [[ -z "$site" ]]; then
    report FAIL "PUBLIC_BASE_URL is not set"
  else
    for path in /healthz /oferta /privacy /tma/; do
      code="$(curl --silent --max-time 10 --output /dev/null --write-out '%{http_code}' "$site$path" || true)"
      [[ "$code" =~ ^[1-5][0-9][0-9]$ ]] || code="no answer"
      if [[ "$code" == 200 ]]; then
        report OK "$site$path"
      else
        report FAIL "$site$path: $code. Bot links (offer, privacy policy, Mini App) lead here"
      fi
    done
  fi
  # A copy of everything that cannot be re-created, kept encrypted and sent off the server.
  row="$(bash "$(dirname "$SELF")/backup_status.sh" 2>/dev/null || true)"
  case "$row" in OK\ * | WARN\ *) report "${row%% *}" "${row#* }" ;; *) report WARN "backup status could not be read" ;; esac
  # Weekly cleanup of old images, build cache and copies (scripts/install_cleanup.sh).
  row="$(bash "$(dirname "$SELF")/cleanup_status.sh" 2>/dev/null || true)"
  case "$row" in OK\ * | WARN\ *) report "${row%% *}" "${row#* }" ;; *) report WARN "cleanup status could not be read" ;; esac
  # Monthly update of system packages and base images, and a pending reboot (scripts/install_auto_update.sh).
  row="$(bash "$(dirname "$SELF")/auto_update_status.sh" 2>/dev/null || true)"
  case "$row" in OK\ * | WARN\ *) report "${row%% *}" "${row#* }" ;; *) report WARN "auto-update status could not be read" ;; esac
  # SSH: password login off, keys only (scripts/ssh_hardening.sh).
  row="$(bash "$(dirname "$SELF")/ssh_hardening.sh" --status 2>/dev/null || true)"
  case "$row" in OK\ * | WARN\ *) report "${row%% *}" "${row#* }" ;; *) report WARN "ssh status could not be read (run as root)" ;; esac
  say "DOCTOR: $ok ok, $warn warnings, $bad problems"
  exit $((bad > 0 ? 1 : 0))
fi

if [[ "${1:-}" == "--set-public-url" ]]; then
  # The bot builds the Mini App, offer and privacy links from this address, so
  # it is switched only after the site answers there.
  url="${2:-}"
  url="${url%/}"
  [[ "$url" =~ ^https://[a-z0-9-]+(\.[a-z0-9-]+)+$ ]] \
    || fail "usage: --set-public-url https://app.cyberdefency.com"
  for path in /healthz /oferta /privacy /tma/; do
    code="$(curl --silent --max-time 10 --output /dev/null --write-out '%{http_code}' "$url$path" || true)"
    [[ "$code" =~ ^[1-5][0-9][0-9]$ ]] || code="no answer"
    [[ "$code" == 200 ]] \
      || fail "$url$path: $code; the address is unchanged. Update the site first: bash scripts/update_web.sh <folder> --apply"
  done
  ALLOW_PUBLIC_URL=1
  set -- --set "PUBLIC_BASE_URL=$url"
fi

if [[ "${1:-}" == "--disable-usdt" ]]; then
  # USDT is not in the offer. The bot reads the Crypto Pay token from the Docker
  # secret secrets/crypto_pay_token.txt first (a secret file overrides .env), so
  # both places are emptied. Copies of both files allow a rollback.
  ENV_FILE="$WORKDIR/.env"
  SECRET_FILE="$WORKDIR/secrets/crypto_pay_token.txt"
  [[ -f "$ENV_FILE" && -f "$SECRET_FILE" ]] || fail "$ENV_FILE and $SECRET_FILE are required"
  env_token="$(grep -c '^CRYPTO_PAY_TOKEN=.' "$ENV_FILE" || true)"
  if [[ ! -s "$SECRET_FILE" && "$env_token" == "0" ]]; then
    say "USDT_ALREADY_OFF"
    exit 0
  fi
  ENV_BACKUP="$ENV_FILE.before-usdt-off-$STAMP"
  SECRET_BACKUP="$SECRET_FILE.bak-usdt-$STAMP"
  cp -p "$ENV_FILE" "$ENV_BACKUP"
  cp -p "$SECRET_FILE" "$SECRET_BACKUP"
  grep -v '^CRYPTO_PAY_TOKEN=' "$ENV_FILE" >"$ENV_FILE.tmp-$STAMP" || true
  cat "$ENV_FILE.tmp-$STAMP" >"$ENV_FILE"  # keeps owner and permissions
  rm -f "$ENV_FILE.tmp-$STAMP"
  : >"$SECRET_FILE"  # emptied in place: the mount keeps its inode and permissions
  say "BACKUPS $ENV_BACKUP $SECRET_BACKUP"
  recreate
  usdt_active=""
  if wait_healthy; then
    usdt_active="$(docker exec "$CONTAINER" python -c 'import sys; sys.path.insert(0, "/app"); from config import get_settings; print(bool(get_settings().crypto_pay_token))' 2>/dev/null || true)"
    if [[ "$usdt_active" == "False" ]]; then
      say "USDT_OFF"
      exit 0
    fi
  fi
  cp -p "$ENV_BACKUP" "$ENV_FILE"
  cp -p "$SECRET_BACKUP" "$SECRET_FILE"
  recreate
  wait_healthy && fail "USDT still active (check said '$usdt_active'); previous files restored" \
    || fail "previous files restored, but the bot is not healthy"
fi

if [[ "${1:-}" == "--set-secret" ]]; then
  # The value is typed on the server, hidden; it never appears in the chat,
  # the command line, shell history or the output.
  key="${2:-}"
  [[ "$key" == TELEGRAM_PAYMENT_TOKEN || "$key" == AWG_AGENT_TOKEN || "$key" == SMTP_PASSWORD ]] \
    || fail "usage: --set-secret TELEGRAM_PAYMENT_TOKEN | AWG_AGENT_TOKEN | SMTP_PASSWORD"
  if [[ -r /dev/tty && -z "${SECRET_FROM_STDIN:-}" ]]; then
    printf 'Вставьте значение %s и нажмите Enter (ввод не отображается): ' "$key" >/dev/tty
    IFS= read -rs secret_value </dev/tty
    printf '\n' >/dev/tty
  else
    IFS= read -r secret_value
  fi
  secret_value="${secret_value//[$'\r\n\t ']/}"
  [[ -n "$secret_value" ]] || fail "empty value"
  if [[ "$key" == TELEGRAM_PAYMENT_TOKEN && ! "$secret_value" =~ ^[0-9]{3,20}:[A-Za-z0-9_:-]{2,256}$ ]]; then
    fail "this does not look like a BotFather payment token (digits:LIVE:... or digits:TEST:...)"
  fi
  chmod 600 "$WORKDIR/.env" 2>/dev/null || true
  set -- --set "$key=$secret_value"
  unset secret_value
fi

# Writes the token file: the value whole, without a trailing newline, owner-only and
# owned by the bot's user (the container runs as uid 1000). An empty value is the
# local file, as init_secrets.sh leaves it. Expects SECRET_FILE to be set.
write_secret_file() {
  (umask 077 && printf '%s' "$1" >"$SECRET_FILE.new")
  chown 1000:1000 "$SECRET_FILE.new" 2>/dev/null || true
  chmod 600 "$SECRET_FILE.new"
  mv "$SECRET_FILE.new" "$SECRET_FILE"
}

# The gateway's token is a Docker secret, like the other keys: it lives in
# secrets/db_gateway_token.txt and never in .env. The live compose file must
# declare that secret; the check below says how to add it if it does not.
if [[ "${1:-}" == "--database-gateway" ]]; then
  # The bot's database moves behind db.gateway on the other server
  # (docs/RU_DATABASE_PLAN.md, section 11). If the bot does not become healthy on
  # it, the previous .env and secret come back: the bot is on the local file again.
  url="${2:-}"
  # The same rule as the bot's settings: plain http only to a private or loopback address.
  [[ "$url" =~ ^http://(127|10|192\.168|172\.(1[6-9]|2[0-9]|3[01]))(\.[0-9]{1,3}){2,3}:[0-9]{4,5}$ ]] \
    || fail "usage: ... | --database-gateway http://10.204.0.1:8444  (an address inside the tunnel)"
  [[ ! -t 0 ]] || fail "the token must arrive on standard input, relayed from the other server"
  IFS= read -r secret_value || true
  secret_value="${secret_value%$'\r'}"
  [[ "$secret_value" =~ ^[A-Za-z0-9_-]{32,128}$ ]] \
    || fail "no token on standard input (expected 32+ letters and digits)"
  ENV_FILE="$WORKDIR/.env"
  SECRET_FILE="$WORKDIR/secrets/db_gateway_token.txt"
  [[ -f "$ENV_FILE" ]] || fail "$ENV_FILE not found"
  [[ -d "$WORKDIR/secrets" ]] || fail "$WORKDIR/secrets not found"
  grep -q 'db_gateway_token' "$WORKDIR/docker-compose.yml" 2>/dev/null \
    || fail "the bot's docker-compose.yml does not declare the db_gateway_token secret yet: compare it with $(dirname "$SELF")/../docker-compose.yml, copy the new file in, then run this again (docs/RU_DATABASE_CUTOVER.md)"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
  ENV_BACKUP="$ENV_FILE.before-set-$STAMP"
  SECRET_BACKUP="$SECRET_FILE.before-set-$STAMP"
  cp -p "$ENV_FILE" "$ENV_BACKUP"
  if [[ -f "$SECRET_FILE" ]]; then cp -p "$SECRET_FILE" "$SECRET_BACKUP"; fi
  # Written before the bot is recreated; the copies of the old settings go below.
  write_secret_file "$secret_value"
  unset secret_value
  grep -vE '^(DATABASE_URL|DB_GATEWAY_TOKEN)=' "$ENV_FILE" >"$ENV_FILE.tmp-$STAMP" || true
  printf 'DATABASE_URL=%s\n' "$url" >>"$ENV_FILE.tmp-$STAMP"
  cat "$ENV_FILE.tmp-$STAMP" >"$ENV_FILE"  # keeps owner and permissions
  rm -f "$ENV_FILE.tmp-$STAMP"
  say "ENV_BACKUP $ENV_BACKUP"
  recreate
  if wait_healthy; then
    # The copies still hold the old .env, and with it the old token: they go.
    rm -f "$ENV_BACKUP" "$SECRET_BACKUP"
    say "DATABASE_GATEWAY_OK: the bot reads its database through the gateway; the token is in secrets/db_gateway_token.txt."
    exit 0
  fi
  cp -p "$ENV_BACKUP" "$ENV_FILE"
  if [[ -f "$SECRET_BACKUP" ]]; then mv "$SECRET_BACKUP" "$SECRET_FILE"; else write_secret_file ""; fi
  rm -f "$ENV_BACKUP"
  recreate
  wait_healthy && fail "the bot is not healthy on the gateway; previous settings are back (healthy)" \
    || fail "previous settings are back, but the bot is not healthy"
fi

if [[ "${1:-}" == "--database-local" ]]; then
  ENV_FILE="$WORKDIR/.env"
  SECRET_FILE="$WORKDIR/secrets/db_gateway_token.txt"
  [[ -f "$ENV_FILE" ]] || fail "$ENV_FILE not found"
  grep -q '^DATABASE_URL=.' "$ENV_FILE" \
    || fail "DATABASE_URL is not set: the bot already uses the local file"
  [[ -s "$WORKDIR/data/bot.db" ]] || fail "$WORKDIR/data/bot.db is missing: there is no local file to go back to"
  ENV_BACKUP="$ENV_FILE.before-set-$STAMP"
  SECRET_BACKUP="$SECRET_FILE.before-set-$STAMP"
  cp -p "$ENV_FILE" "$ENV_BACKUP"
  if [[ -f "$SECRET_FILE" ]]; then cp -p "$SECRET_FILE" "$SECRET_BACKUP"; fi
  grep -vE '^(DATABASE_URL|DB_GATEWAY_TOKEN)=' "$ENV_FILE" >"$ENV_FILE.tmp-$STAMP" || true
  cat "$ENV_FILE.tmp-$STAMP" >"$ENV_FILE"  # keeps owner and permissions
  rm -f "$ENV_FILE.tmp-$STAMP"
  # The token leaves the disk with the gateway: an empty secret means the local file.
  write_secret_file ""
  say "ENV_BACKUP $ENV_BACKUP"
  recreate
  if wait_healthy; then
    rm -f "$ENV_BACKUP" "$SECRET_BACKUP"
    say "DATABASE_LOCAL_OK: the bot uses the local file again."
    say "Records made while it used the gateway are NOT in this file: they stay on the other server."
    exit 0
  fi
  cp -p "$ENV_BACKUP" "$ENV_FILE"
  if [[ -f "$SECRET_BACKUP" ]]; then mv "$SECRET_BACKUP" "$SECRET_FILE"; else write_secret_file ""; fi
  rm -f "$ENV_BACKUP"
  recreate
  wait_healthy && fail "the bot is not healthy on the local file; the gateway settings are back (healthy)" \
    || fail "the gateway settings are back, but the bot is not healthy"
fi

if [[ "${1:-}" == "--set-tiers" ]]; then
  set -- --set "ENABLED_TIERS=${2:-}"
fi
if [[ "${1:-}" == "--set" ]]; then
  shift
  (($# > 0)) || fail "usage: --set KEY=VALUE [KEY=VALUE ...]  keys: $SETTABLE_KEYS"
  for pair in "$@"; do
    key="${pair%%=*}"
    value="${pair#*=}"
    [[ "$pair" == *=* && ( " $SETTABLE_KEYS " == *" $key "* \
      || ( "$key" == PUBLIC_BASE_URL && "${ALLOW_PUBLIC_URL:-0}" == 1 ) ) ]] \
      || fail "not allowed: $key (allowed: $SETTABLE_KEYS)"
    # MARZBAN_EXTRA_INBOUND_TAGS may be emptied: no extra entry for new users.
    [[ ( -n "$value" || "$key" == MARZBAN_EXTRA_INBOUND_TAGS ) && "$value" != *$'\n'* && "$value" != *[\"\'\\\$\`]* ]] \
      || fail "invalid value for $key"
    if [[ "$key" == MARZBAN_EXTRA_INBOUND_TAGS && ! "$value" =~ ^([A-Za-z0-9_-]{3,40}(,[A-Za-z0-9_-]{3,40})*)?$ ]]; then
      fail "usage: --set MARZBAN_EXTRA_INBOUND_TAGS=VLESS-GRPC-REALITY  (tags without spaces, comma-separated; empty for none)"
    fi
    if [[ "$key" == ACTIVITY_LOGGING_ENABLED && ! "$value" =~ ^(true|false)$ ]]; then
      fail "usage: --set ACTIVITY_LOGGING_ENABLED=false"
    fi
    if [[ "$key" == AUTOBAN_MODE && ! "$value" =~ ^(review|automatic)$ ]]; then
      fail "usage: --set AUTOBAN_MODE=review"
    fi
    if [[ "$key" == CONN_LOG_RETENTION_DAYS && ! "$value" =~ ^[1-9][0-9]{1,4}$ ]]; then
      fail "usage: --set CONN_LOG_RETENTION_DAYS=1826  (days; 1826 is five years)"
    fi
    if [[ "$key" == SERVER_TRAFFIC_LIMIT_GB && ! "$value" =~ ^([0-9]{1,6}|1000000)$ ]]; then
      fail "usage: --set SERVER_TRAFFIC_LIMIT_GB=5000  (decimal GB, 0 = off)"
    fi
    if [[ "$key" == SERVER_TRAFFIC_COUNTS && ! "$value" =~ ^(both|out|in)$ ]]; then
      fail "usage: --set SERVER_TRAFFIC_COUNTS=both  (both, out or in)"
    fi
    if [[ "$key" == SERVER_TRAFFIC_RESET_DAY && ! "$value" =~ ^([1-9]|1[0-9]|2[0-8])$ ]]; then
      fail "usage: --set SERVER_TRAFFIC_RESET_DAY=1  (1..28)"
    fi
    if [[ "$key" == SERVER_TRAFFIC_PAUSE_TRIALS && ! "$value" =~ ^(true|false)$ ]]; then
      fail "usage: --set SERVER_TRAFFIC_PAUSE_TRIALS=false"
    fi
    if [[ "$key" == RU_DIRECT_SUBSCRIPTION && ! "$value" =~ ^(true|false)$ ]]; then
      fail "usage: --set RU_DIRECT_SUBSCRIPTION=true  (set by scripts/ru_direct.sh)"
    fi
    if [[ "$key" == YOOKASSA_CONFIRMATION && ! "$value" =~ ^(redirect|qr)$ ]]; then
      fail "usage: --set YOOKASSA_CONFIRMATION=qr  (qr: the bot shows the SBP QR code; redirect: the YooMoney page)"
    fi
    if [[ "$key" == ENABLED_TIERS ]]; then
      [[ "$value" =~ ^(standard|basic|complete|prime)(,(standard|basic|complete|prime))*$ ]] \
        || fail "usage: --set ENABLED_TIERS=standard  (standard, basic, complete, prime)"
    fi
  done
  for pair in "$@"; do
    # Switch to QR only when YooKassa really answers with an SBP link: a refusal
    # here would otherwise stop every ruble payment.
    [[ "$pair" == YOOKASSA_CONFIRMATION=qr ]] || continue
    say "Checking that YooKassa gives SBP payments by QR (a 10 RUB test payment nobody can pay)..."
    payment_qr_check || fail "the QR check did not pass; nothing was changed"
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
    # Keys only: a token value must not end up in the terminal or a log.
    say "SET_OK $(for pair in "$@"; do printf '%s ' "${pair%%=*}"; done)"
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
# Signed releases (docs/SIGNED_RELEASES.md): once the guard is installed, only the
# code the owner signed is built.
RELEASE_GUARD="${RELEASE_GUARD_BIN:-/usr/local/sbin/vpn-release-guard}"
if [[ -x "$RELEASE_GUARD" ]]; then
  "$RELEASE_GUARD" --verify-head "$SOURCE" || exit 1
fi
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
[[ -f "$ENV_FILE" ]] || fail "$ENV_FILE not found"
# With DATABASE_URL the live database is on the other server, behind db.gateway.
# The local file is then an old copy: backing it up or testing the new schema on
# it would say nothing about the real data.
REMOTE_DB=0
if grep -q '^DATABASE_URL=.' "$ENV_FILE"; then
  REMOTE_DB=1
else
  [[ -s "$DATA_DIR/bot.db" ]] || fail "$DATA_DIR/bot.db not found or empty"
  [[ -f "$KEY_FILE" ]] || fail "$KEY_FILE not found (the database key is required)"
fi

say "container=$CONTAINER service=$SERVICE project=$PROJECT"
say "workdir=$WORKDIR image=$IMAGE_REF"

ROLLBACK="node-rental-bot:rollback-$STAMP"
docker tag "$(docker inspect -f '{{.Image}}' "$CONTAINER")" "$ROLLBACK"
say "ROLLBACK_IMAGE_SAVED $ROLLBACK"

NEW="node-rental-bot:update-$STAMP"
# The image carries a digest of what it is built from, so the monthly
# auto-update can tell whether the checkout still holds the code that runs.
DIGEST="$(bash "$(dirname "$SELF")/source_digest.sh" "$SOURCE" 2>/dev/null || true)"
docker build --target bot --label "node.rental.source=$DIGEST" -t "$NEW" "$SOURCE" \
  || fail "build failed; nothing was changed"
say "BUILD_OK $NEW"

# The new image's tool works whatever the old image contains. SQLite's online
# backup is safe while the live bot keeps writing.
run_tool() {
  docker run --rm --user 1000:1000 --read-only --tmpfs /tmp --network none \
    -v "$DATA_DIR:/app/data" -v "$KEY_FILE:/run/secrets/db_key:ro" \
    "$NEW" python scripts/db_tool.py "$@"
}
if ((REMOTE_DB)); then
  say "DB_REMOTE: the database is on the other server. Update the gateway there FIRST"
  say "  (db_gateway_install.sh --apply): it applies schema changes, and this bot"
  say "  refuses a gateway older than itself. No local backup or schema check was made."
else
  BACKUP="before-update-$STAMP.db"
  run_tool backup /app/data/bot.db "/app/data/$BACKUP" \
    || fail "database backup failed; nothing was changed"
  say "DB_BACKUP_OK $DATA_DIR/$BACKUP"

  run_tool migrate-check /app/data/bot.db \
    || fail "migration check failed on the copy; nothing was changed"
fi

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
  add_env YOOKASSA_IN_BOT true
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
