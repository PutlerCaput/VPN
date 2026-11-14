#!/usr/bin/env bash
# Database gateway on the RU server (docs/RU_DATABASE_PLAN.md, section 11).
#   bash db_gateway_install.sh            report: what is in place; changes nothing
#   bash db_gateway_install.sh --apply    packages, service user, folders, Python
#                                         environment, code, access token, systemd
#                                         unit. Does NOT start the service
#   bash db_gateway_install.sh --start    check the database file, its key and the
#                                         tunnel, then start and enable the service
#   bash db_gateway_install.sh --stop     stop and disable the service; data stays
#   bash db_gateway_install.sh --status   OK or WARN: the service, the monthly archive
#   bash db_gateway_install.sh --archive-key age1...
#                                         the owner's PUBLIC key for the five-year
#                                         archive; switches the monthly timer on
#   bash db_gateway_install.sh --connection-log on|off
#                                         the connection records switch, next to the
#                                         data. On only with the policy that names it
#   bash db_gateway_install.sh --rotate-token
#                                         step 1 of a token change: make a new token
#                                         (never shown) and accept it next to the old
#   bash db_gateway_install.sh --finish-token-rotation
#                                         step 3: drop the old token, once the bot is
#                                         seen working with the new one
#   bash db_gateway_install.sh --cancel-token-rotation
#                                         give the new token up and keep the old one
#   (step 2 is on the other server: docs/RU_DATABASE_CUTOVER.md, "Смена токена")
#
# Run it from the folder the files were copied to (it installs the code that
# lies next to it). No Docker: a Python environment and a systemd unit.
# --apply can be repeated: it updates the code and keeps the token, the
# settings file, the key and the database.
set -euo pipefail

PREFIX="${PREFIX:-}"                     # tests install under a temporary root
SERVICE_USER="${SERVICE_USER:-vpn-db}"
OPT="$PREFIX/opt/vpn-db"
APP="$OPT/app"
VENV="$OPT/venv"
ETC="$PREFIX/etc/vpn-db"
SECRETS="$ETC/secrets"
DATA="$PREFIX/var/lib/vpn-db"
DB_FILE="$DATA/bot.db"
ENV_FILE="$ETC/gateway.env"
TOKEN_FILE="$SECRETS/db_gateway_token"
NEXT_FILE="$SECRETS/db_gateway_token_next"       # exists only during a token change
PREVIOUS_FILE="$SECRETS/db_gateway_token.finishing"
KEY_FILE="$SECRETS/db_key"
UNIT="node-db-gateway.service"
UNIT_DIR="$PREFIX/etc/systemd/system"
ARCHIVE_UNIT="node-retention-archive"
ARCHIVE_ENV="$ETC/retention.env"
ARCHIVE_DIR="$DATA/archive"
LISTEN_HOST="${DB_GATEWAY_HOST:-10.204.0.1}" # this server's end of the wg-db tunnel
LISTEN_PORT="${DB_GATEWAY_PORT:-8444}"
PYTHON="${PYTHON_BIN:-python3}"

SRC="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
say() { printf '%s\n' "$*"; }
stop() {
  printf 'DB_GATEWAY_STOPPED: %s\n' "$*" >&2
  exit 1
}
# Not yet, but nothing is wrong: try again in a minute. db_cutover.sh waits on 3.
later() {
  printf 'DB_GATEWAY_TOKEN_NOT_YET: %s\n' "$*" >&2
  exit 3
}
need() { command -v "$1" >/dev/null 2>&1 || stop "missing command: $1"; }
as_root() { [[ "$(id -u)" == 0 ]] || stop "run as root"; }

source_complete() {
  local f
  for f in db/__init__.py db/database.py db/endpoint.py db/gateway.py logging_setup.py \
    scripts/retention_archiver.py deploy/db-gateway/requirements.txt \
    "deploy/db-gateway/$UNIT" "deploy/db-gateway/$ARCHIVE_UNIT.service" \
    "deploy/db-gateway/$ARCHIVE_UNIT.timer"; do
    [[ -s "$SRC/$f" ]] || stop "the copy is incomplete: $SRC/$f is missing"
  done
}

health() {
  # Prints ok when the service answers on the tunnel address.
  "$VENV/bin/python" - "$LISTEN_HOST" "$LISTEN_PORT" <<'PY' 2>/dev/null || true
import sys
import urllib.request

try:
    with urllib.request.urlopen(f"http://{sys.argv[1]}:{sys.argv[2]}/healthz", timeout=3) as r:
        print(r.read().decode().strip())
except OSError:
    pass
PY
}

mark() { [[ "$1" == yes ]] && printf 'present' || printf 'MISSING'; }
have() { [[ -e "$1" ]] && printf yes || printf no; }

report() {
  say "== Database gateway on this server"
  say "  source copy: $SRC"
  say "  python: $("$PYTHON" --version 2>&1 || echo missing)"
  say "  service user $SERVICE_USER: $(id "$SERVICE_USER" >/dev/null 2>&1 && echo present || echo MISSING)"
  say "  code $APP: $(mark "$(have "$APP/db/gateway.py")")"
  say "  python environment $VENV: $(mark "$(have "$VENV/bin/python")")"
  say "  settings $ENV_FILE: $(mark "$(have "$ENV_FILE")")"
  say "  access token: $(mark "$(have "$TOKEN_FILE")")"
  if [[ -e "$NEXT_FILE" ]]; then
    say "  token change: OPEN, two tokens are accepted (--finish-token-rotation or --cancel-token-rotation)"
  else
    say "  token change: none open"
  fi
  say "  database key: $(mark "$(have "$KEY_FILE")")"
  if [[ -s "$DB_FILE" ]]; then
    say "  database file: present, $(du -h "$DB_FILE" | cut -f1)"
  else
    say "  database file: MISSING ($DB_FILE)"
  fi
  # is-active prints the state itself and fails when it is not "active".
  say "  unit: $(mark "$(have "$UNIT_DIR/$UNIT")"), $(systemctl is-active "$UNIT" 2>/dev/null || true)"
  say "  free memory: $(free -m 2>/dev/null | awk '/^Mem:/ {print $7 " MB available of " $2}')"
  say "  free disk: $(df -h --output=avail "$PREFIX/" 2>/dev/null | tail -1 | tr -d ' ')"
  say "DB_GATEWAY_REPORT_DONE"
}

apply() {
  as_root
  need "$PYTHON"
  need install
  source_complete
  if ! "$PYTHON" -c 'import ensurepip, venv' >/dev/null 2>&1; then
    say "installing python3-venv..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-venv >/dev/null \
      || stop "could not install python3-venv"
  fi
  if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$SERVICE_USER" \
      || stop "could not create the user $SERVICE_USER"
  fi
  # Code and settings: root writes, the service reads. Data: the service only.
  install -d -m 0755 "$OPT" "$APP" "$APP/db" "$UNIT_DIR"
  install -d -m 0750 -o root -g "$SERVICE_USER" "$ETC" "$SECRETS"
  install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA"
  rm -f "$APP"/db/*.py
  install -m 0644 "$SRC"/db/*.py "$APP/db/"
  install -m 0644 "$SRC/logging_setup.py" "$APP/logging_setup.py"
  install -d -m 0755 "$APP/scripts"
  install -m 0644 "$SRC/scripts/retention_archiver.py" "$APP/scripts/retention_archiver.py"
  if [[ -f "$SRC/scripts/db_tool.py" ]]; then
    install -m 0644 "$SRC/scripts/db_tool.py" "$APP/scripts/db_tool.py"
  fi
  if ! command -v age >/dev/null 2>&1 || ! command -v age-keygen >/dev/null 2>&1; then
    say "installing age (the archive is encrypted with it)..."
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q age >/dev/null \
      || say "WARN: age could not be installed; the monthly archive will not run until it is"
  fi

  [[ -x "$VENV/bin/python" ]] || "$PYTHON" -m venv "$VENV" || stop "could not create $VENV"
  say "installing Python packages (needs pypi.org)..."
  "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check \
    -r "$SRC/deploy/db-gateway/requirements.txt" \
    || stop "pip could not install the packages (is pypi.org reachable from here?)"
  # The real thing, not just an import: open an encrypted database and read it back.
  (cd "$APP" && "$VENV/bin/python" - <<'PY') || stop "the self-test failed: the environment is not usable"
import asyncio
import os
import tempfile

from db.database import Database
from db.gateway import remote_methods


async def main() -> None:
    with tempfile.TemporaryDirectory() as folder:
        path = os.path.join(folder, "selftest.db")
        first = Database(path, "ab" * 32)
        await first.connect()
        await first.create_user(1, "selftest", None)
        await first.close()
        again = Database(path, "ab" * 32)
        await again.connect()
        assert (await again.get_user(1))["username"] == "selftest"
        await again.close()
        with open(path, "rb") as raw:
            assert not raw.read(16).startswith(b"SQLite format 3")
    assert len(remote_methods()) > 100


asyncio.run(main())
PY

  if [[ ! -s "$TOKEN_FILE" ]]; then
    (umask 077 && head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' >"$TOKEN_FILE.new")
    [[ "$(wc -c <"$TOKEN_FILE.new")" == 64 ]] || stop "could not create the access token"
    mv "$TOKEN_FILE.new" "$TOKEN_FILE"
    say "access token: created (it is never shown; copy it to DE through the phone)"
  else
    say "access token: kept"
  fi
  chown "root:$SERVICE_USER" "$TOKEN_FILE"
  chmod 0440 "$TOKEN_FILE"
  if [[ ! -f "$ENV_FILE" ]]; then
    cat >"$ENV_FILE" <<EOF
# Settings of the database gateway. Secrets are files in $SECRETS, not here.
SECRETS_DIR=$SECRETS
DATABASE_PATH=$DB_FILE
DB_GATEWAY_HOST=$LISTEN_HOST
DB_GATEWAY_PORT=$LISTEN_PORT
# The connection log. Stays false until the updated privacy policy is published.
ACTIVITY_LOGGING_ENABLED=false
LOG_LEVEL=INFO
EOF
    say "settings: written to $ENV_FILE"
  else
    say "settings: kept ($ENV_FILE)"
  fi
  chown "root:$SERVICE_USER" "$ENV_FILE"
  chmod 0640 "$ENV_FILE"
  install -m 0644 "$SRC/deploy/db-gateway/$UNIT" "$UNIT_DIR/$UNIT"
  install -m 0644 "$SRC/deploy/db-gateway/$ARCHIVE_UNIT.service" "$UNIT_DIR/$ARCHIVE_UNIT.service"
  install -m 0644 "$SRC/deploy/db-gateway/$ARCHIVE_UNIT.timer" "$UNIT_DIR/$ARCHIVE_UNIT.timer"
  install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$ARCHIVE_DIR"
  systemctl daemon-reload || stop "systemctl daemon-reload failed"
  if [[ "$(systemctl is-active "$UNIT" 2>/dev/null || true)" == active ]]; then
    systemctl restart "$UNIT" || stop "the new code is installed but the service did not restart"
    say "service: restarted with the new code"
  fi
  say "DB_GATEWAY_INSTALLED: code, environment, token and unit are in place."
  say "Not started. It still needs: the tunnel (db_tunnel.sh), the database file"
  say "  $DB_FILE  and its key  $KEY_FILE , then:  bash $0 --start"
}

start() {
  as_root
  [[ -x "$VENV/bin/python" && -f "$UNIT_DIR/$UNIT" ]] || stop "not installed yet: run --apply first"
  [[ -s "$TOKEN_FILE" ]] || stop "the access token is missing: run --apply"
  # An empty or missing file here would hand the bot an empty database.
  [[ -s "$DB_FILE" ]] || stop "the database file is missing or empty: $DB_FILE"
  [[ -s "$KEY_FILE" ]] || stop "the database key is missing: $KEY_FILE"
  [[ "$(tr -d '\n' <"$KEY_FILE" | wc -c)" == 64 ]] || stop "the database key must be 64 hex characters"
  chown "$SERVICE_USER:$SERVICE_USER" "$DB_FILE"
  chmod 0600 "$DB_FILE"
  chown "root:$SERVICE_USER" "$KEY_FILE"
  chmod 0440 "$KEY_FILE"
  ip -4 -o addr show 2>/dev/null | grep -q " $LISTEN_HOST/" \
    || stop "the tunnel address $LISTEN_HOST is not up: bash db_tunnel.sh --status"
  # The key must open the file before the bot is pointed here.
  local checked
  checked="$(cd "$APP" && SECRETS_DIR="$SECRETS" "$VENV/bin/python" scripts/db_tool.py check "$DB_FILE" 2>&1)" \
    || stop "the key does not open the database file, or the file is damaged"
  say "database check: $checked"
  systemctl enable "$UNIT" >/dev/null 2>&1 || stop "could not enable $UNIT"
  systemctl restart "$UNIT" || stop "could not start $UNIT (journalctl -u $UNIT -n 20)"
  local n
  for n in 1 2 3 4 5 6 7 8 9 10; do
    if [[ "$(health)" == ok ]]; then
      say "DB_GATEWAY_STARTED: answering on $LISTEN_HOST:$LISTEN_PORT and enabled on boot."
      return
    fi
    sleep 1
  done
  stop "started, but no answer on $LISTEN_HOST:$LISTEN_PORT after ${n}s (journalctl -u $UNIT -n 20)"
}

halt() {
  as_root
  systemctl disable --now "$UNIT" >/dev/null 2>&1 || true
  say "DB_GATEWAY_HALTED: the service is stopped and off on boot. Data, key and token are kept."
}

connection_log_now() {
  local value
  value="$(grep '^ACTIVITY_LOGGING_ENABLED=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2)"
  [[ "$value" == true ]] && printf on || printf off
}

status() {
  if [[ "$(systemctl is-active "$UNIT" 2>/dev/null || true)" != active ]]; then
    say "WARN gateway: $UNIT is not running"
  elif [[ "$(health)" == ok ]]; then
    say "OK gateway: running and answering on $LISTEN_HOST:$LISTEN_PORT"
  else
    say "WARN gateway: running but not answering on $LISTEN_HOST:$LISTEN_PORT"
  fi
  if [[ -e "$NEXT_FILE" ]]; then
    say "WARN token change: open, the gateway accepts two tokens (--finish-token-rotation, or --cancel-token-rotation)"
  fi
  [[ -f "$ENV_FILE" ]] && say "OK connection records: $(connection_log_now)"
  if [[ ! -s "$ARCHIVE_ENV" ]]; then
    say "WARN retention archive: the owner's public key is not set (--archive-key age1...)"
  elif [[ -x "$VENV/bin/python" ]]; then
    "$VENV/bin/python" "$APP/scripts/retention_archiver.py" --archive-dir "$ARCHIVE_DIR" --status
  fi
}

archive_key() {
  as_root
  local recipient="$1"
  [[ "$recipient" =~ ^age1[0-9a-z]{58}$ ]] \
    || stop "that is not an age PUBLIC key (age1 followed by 58 characters)"
  [[ -f "$UNIT_DIR/$ARCHIVE_UNIT.timer" ]] || stop "not installed yet: run --apply first"
  command -v age >/dev/null 2>&1 || stop "age is not installed: apt-get install -y age"
  printf '# The owner'"'"'s PUBLIC key: archives are sealed to it, only the owner opens them.\nAGE_RECIPIENT=%s\n' \
    "$recipient" >"$ARCHIVE_ENV.new"
  chown "root:$SERVICE_USER" "$ARCHIVE_ENV.new"
  chmod 0640 "$ARCHIVE_ENV.new"
  mv "$ARCHIVE_ENV.new" "$ARCHIVE_ENV"
  systemctl enable --now "$ARCHIVE_UNIT.timer" >/dev/null 2>&1 || stop "could not switch the monthly timer on"
  say "DB_GATEWAY_ARCHIVE_ON: closed months are sealed to this key on the 2nd of each month."
  say "Records are kept 5 years; only the owner's private key opens an archive."
}

connection_log() {
  as_root
  local want="$1" value
  [[ "$want" == on || "$want" == off ]] || stop "usage: --connection-log on|off"
  [[ -f "$ENV_FILE" ]] || stop "not installed yet: run --apply first"
  [[ "$want" == on ]] && value=true || value=false
  if [[ "$(connection_log_now)" == "$want" ]]; then
    say "DB_GATEWAY_CONNECTION_LOG $want (already)"
    return
  fi
  grep -v '^ACTIVITY_LOGGING_ENABLED=' "$ENV_FILE" >"$ENV_FILE.new" || true
  printf 'ACTIVITY_LOGGING_ENABLED=%s\n' "$value" >>"$ENV_FILE.new"
  chown "root:$SERVICE_USER" "$ENV_FILE.new"
  chmod 0640 "$ENV_FILE.new"
  mv "$ENV_FILE.new" "$ENV_FILE"
  if [[ "$(systemctl is-active "$UNIT" 2>/dev/null || true)" == active ]]; then
    systemctl restart "$UNIT" || stop "the setting is saved but the service did not restart"
  fi
  say "DB_GATEWAY_CONNECTION_LOG $want"
}

wait_health() {
  local n
  for n in 1 2 3 4 5 6 7 8 9 10; do
    [[ "$(health)" == ok ]] && return 0
    sleep 1
  done
  return 1
}

# How the tokens are used, from /v1/info asked with the token in the file $1:
#   <tokens accepted> <seconds up> <calls, old> <idle, old> <calls, new> <idle, new>
# "-" means none. Only work counts as use: this question does not. The token is
# read from the file by the helper: it is on no command line and no screen.
token_facts() {
  "$VENV/bin/python" - "$LISTEN_HOST" "$LISTEN_PORT" "$1" <<'PY' 2>/dev/null || true
import json
import sys
import urllib.request

host, port, path = sys.argv[1:4]
try:
    with open(path, encoding="utf-8") as handle:
        token = handle.read().strip()
    request = urllib.request.Request(
        f"http://{host}:{port}/v1/info", headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        facts = json.load(response)
    use = facts.get("token_use") or {}

    def field(name, key):
        value = (use.get(name) or {}).get(key)
        return "-" if value is None else str(int(value))

    print(
        int(facts["tokens_accepted"]),
        int(facts["uptime_seconds"]),
        field("current", "calls"),
        field("current", "idle_seconds"),
        field("next", "calls"),
        field("next", "idle_seconds"),
    )
except Exception:
    pass
PY
}

# Seconds a token must have been unused before the other one is dropped. The bot
# asks the gateway every few seconds, so a minute or two of silence means it is
# really on the other token. Tests shorten it.
idle_minimum() {
  local value="${TOKEN_ROTATION_IDLE_SECONDS:-120}"
  [[ "$value" =~ ^[0-9]{1,4}$ ]] || stop "TOKEN_ROTATION_IDLE_SECONDS must be a number of seconds"
  printf '%s' "$value"
}

destroy() { shred -u "$1" 2>/dev/null || rm -f "$1"; }

rotate_token() {
  as_root
  [[ -x "$VENV/bin/python" && -f "$UNIT_DIR/$UNIT" ]] || stop "not installed yet: run --apply first"
  [[ -s "$TOKEN_FILE" ]] || stop "the access token is missing: run --apply"
  [[ ! -e "$NEXT_FILE" ]] \
    || stop "a token change is already open: finish it (--finish-token-rotation) or give it up (--cancel-token-rotation)"
  [[ "$(systemctl is-active "$UNIT" 2>/dev/null || true)" == active && "$(health)" == ok ]] \
    || stop "the gateway is not running and answering: start it first (--start)"
  [[ -n "$(token_facts "$TOKEN_FILE")" ]] \
    || stop "the running gateway does not report its tokens: update it first (--apply), then try again"
  (umask 077 && head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' >"$NEXT_FILE.new")
  if [[ "$(wc -c <"$NEXT_FILE.new")" != 64 ]]; then
    destroy "$NEXT_FILE.new"
    stop "could not create the new token"
  fi
  chown "root:$SERVICE_USER" "$NEXT_FILE.new"
  chmod 0440 "$NEXT_FILE.new"
  mv "$NEXT_FILE.new" "$NEXT_FILE"
  # It reads the file at start: a restart of a second or two, like --apply.
  local line tokens
  if systemctl restart "$UNIT" && wait_health; then
    line="$(token_facts "$TOKEN_FILE")"
    tokens="${line%% *}"
    if [[ "$tokens" == 2 && -n "$(token_facts "$NEXT_FILE")" ]]; then
      say "DB_GATEWAY_ROTATION_OPEN: the gateway accepts the old and the new token."
      say "The new token is in $NEXT_FILE and is never shown."
      say "Next, on the other server: take it from there through the phone into"
      say "  update_bot.sh --database-gateway http://$LISTEN_HOST:$LISTEN_PORT"
      say "(docs/RU_DATABASE_CUTOVER.md, \"Смена токена\"), then here: --finish-token-rotation"
      return
    fi
  fi
  # It did not come up with two tokens: back to one, as it was.
  destroy "$NEXT_FILE"
  systemctl restart "$UNIT" || true
  wait_health || true
  stop "the gateway did not come up accepting two tokens; the change was given up and the old token still works (journalctl -u $UNIT -n 20)"
}

finish_token_rotation() {
  as_root
  [[ -e "$NEXT_FILE" ]] || stop "no token change is open: start one with --rotate-token"
  [[ -x "$VENV/bin/python" ]] || stop "not installed yet: run --apply first"
  local minimum line tokens uptime cur_idle nxt_calls
  minimum="$(idle_minimum)"
  line="$(token_facts "$NEXT_FILE")"
  [[ -n "$line" ]] || stop "the gateway does not answer to the new token: is it running with both tokens? (--status)"
  read -r tokens uptime _ cur_idle nxt_calls _ <<<"$line"
  [[ "$tokens" == 2 ]] \
    || stop "the running gateway accepts $tokens token(s), not two: nothing was changed (--start restarts it)"
  [[ "$nxt_calls" =~ ^[0-9]+$ && "$nxt_calls" -gt 0 ]] \
    || later "nobody has used the new token yet: copy it to the other server first (docs/RU_DATABASE_CUTOVER.md, \"Смена токена\"); nothing was changed"
  ((uptime >= minimum)) \
    || later "the gateway restarted ${uptime}s ago: wait until it has run for ${minimum}s, so that a client still on the old token would have shown itself; nothing was changed"
  if [[ "$cur_idle" =~ ^[0-9]+$ ]] && ((cur_idle < minimum)); then
    later "the old token was used ${cur_idle}s ago: the bot is not on the new token yet; nothing was changed"
  fi
  # The new token becomes the token; the old one is kept only until this works.
  cp -p "$TOKEN_FILE" "$PREVIOUS_FILE"
  mv "$NEXT_FILE" "$TOKEN_FILE"
  if systemctl restart "$UNIT" && wait_health; then
    line="$(token_facts "$TOKEN_FILE")"
    if [[ "${line%% *}" == 1 ]]; then
      destroy "$PREVIOUS_FILE"
      say "DB_GATEWAY_TOKEN_ROTATED: only the new token is accepted now; the old one is destroyed."
      say "Next: the integrity check on this server will show the changed token file; look, then accept."
      return
    fi
  fi
  # Back to two tokens, so the bot is not locked out.
  mv "$TOKEN_FILE" "$NEXT_FILE"
  mv "$PREVIOUS_FILE" "$TOKEN_FILE"
  systemctl restart "$UNIT" || true
  wait_health || true
  stop "the gateway did not come up on the new token alone; both tokens are accepted again (journalctl -u $UNIT -n 20)"
}

cancel_token_rotation() {
  as_root
  [[ -e "$NEXT_FILE" ]] || stop "no token change is open"
  local minimum line nxt_idle
  minimum="$(idle_minimum)"
  line="$(token_facts "$NEXT_FILE")"
  if [[ -n "$line" ]]; then
    read -r _ _ _ _ _ nxt_idle <<<"$line"
    # Giving the new token up would lock out a bot that already works with it.
    if [[ "$nxt_idle" =~ ^[0-9]+$ ]] && ((nxt_idle < minimum)); then
      later "the new token was used ${nxt_idle}s ago: the bot works with it. Finish the change (--finish-token-rotation) instead, or wait and give it up; nothing was changed"
    fi
  fi
  destroy "$NEXT_FILE"
  if [[ "$(systemctl is-active "$UNIT" 2>/dev/null || true)" == active ]]; then
    systemctl restart "$UNIT" || stop "the new token is destroyed but the service did not restart"
    wait_health || stop "the new token is destroyed but the gateway does not answer (journalctl -u $UNIT -n 20)"
  fi
  say "DB_GATEWAY_ROTATION_CANCELLED: only the old token is accepted again; the new one is destroyed."
}

case "${1:-}" in
  "") report ;;
  --apply) apply ;;
  --start) start ;;
  --stop) halt ;;
  --status) status ;;
  --archive-key)
    [[ $# -eq 2 ]] || stop "usage: db_gateway_install.sh --archive-key age1..."
    archive_key "$2"
    ;;
  --connection-log)
    [[ $# -eq 2 ]] || stop "usage: db_gateway_install.sh --connection-log on|off"
    connection_log "$2"
    ;;
  --rotate-token) rotate_token ;;
  --finish-token-rotation) finish_token_rotation ;;
  --cancel-token-rotation) cancel_token_rotation ;;
  *) stop "usage: db_gateway_install.sh [--apply | --start | --stop | --status | --archive-key KEY | --connection-log on|off | --rotate-token | --finish-token-rotation | --cancel-token-rotation]" ;;
esac
