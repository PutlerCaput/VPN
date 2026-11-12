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
  *) stop "usage: db_gateway_install.sh [--apply | --start | --stop | --status | --archive-key KEY | --connection-log on|off]" ;;
esac
