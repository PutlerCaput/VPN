#!/usr/bin/env bash
# Move the bot's database from the DE server to the RU server, from the phone.
# Runs in Termux: it reaches both servers over ssh and carries files between
# them, because the servers cannot reach each other's ssh.
#
#   bash db_cutover.sh check      read-only: is everything ready
#   bash db_cutover.sh prepare    no downtime: code to RU, tunnel, firewall, gateway
#   bash db_cutover.sh switch     the bot is offline for a few minutes: copy the
#                                 database and its key, start the gateway, point
#                                 the bot at it. Takes itself back on failure
#   bash db_cutover.sh rollback   the bot back on the file on DE
#   bash db_cutover.sh status     where the data is and whether it answers
#   bash db_cutover.sh update     later releases: new gateway code to RU (do this
#                                 BEFORE updating the bot on DE)
#   bash db_cutover.sh connection-log on|off
#                                 the connection records switch. "on" is refused
#                                 until the bot publishes the policy that names them
#
# Nothing secret is shown or stored on the phone: the database, its key and the
# access token only pass through, from one ssh to the other.
# docs/RU_DATABASE_CUTOVER.md explains each step.
set -euo pipefail

DE="${DE_SSH:-de}"
RU="${RU_SSH:-root@45.138.157.65}"
DE_SRC="${DE_SRC:-/root/vpn-src}"
RU_SRC="${RU_SRC:-/root/vpn-db-src}"
RU_DATA="${RU_DATA:-/var/lib/vpn-db}"
RU_SECRETS="${RU_SECRETS:-/etc/vpn-db/secrets}"
RU_FIREWALL="${RU_FIREWALL:-/root/ru_firewall.sh}"
RU_PUBLIC="${RU_PUBLIC:-45.138.157.65}"
BACKUP_ENV_DE="${BACKUP_ENV_DE:-/etc/node-backup.env}"
CONTAINER="${CONTAINER:-node-rental-bot-1}"
GATEWAY_URL="${GATEWAY_URL:-http://10.204.0.1:8444}"
TUNNEL_PORT="${DB_TUNNEL_PORT:-51820}"
SSH_BIN="${SSH_BIN:-ssh}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
KEY_RE='^[A-Za-z0-9+/]{43}=$'

say() { printf '%s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
stop() {
  printf 'CUTOVER_STOPPED: %s\n' "$*" >&2
  exit 1
}
# BatchMode: a question from ssh in the middle of a relay would hang it. A key
# with a passphrase therefore has to be loaded first (ssh-add).
de() { "$SSH_BIN" -o BatchMode=yes -o ConnectTimeout=15 "$DE" "$@"; }
ru() { "$SSH_BIN" -o BatchMode=yes -o ConnectTimeout=15 "$RU" "$@"; }

# The bot's folder on DE and the image it runs, as docker knows them.
bot_facts() {
  WORKDIR_DE="$(de "docker inspect -f '{{ index .Config.Labels \"com.docker.compose.project.working_dir\" }}' $CONTAINER")" \
    || stop "DE does not answer, or the container $CONTAINER is not there. Check: ssh $DE true (a key with a passphrase needs ssh-add first)"
  IMAGE_DE="$(de "docker inspect -f '{{.Config.Image}}' $CONTAINER")"
  [[ "$WORKDIR_DE" == /* && -n "$IMAGE_DE" ]] || stop "could not read the bot's folder and image on DE"
}

bot_health() {
  de "docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' $CONTAINER" 2>/dev/null || echo unknown
}

bot_is_remote() { de "grep -q '^DATABASE_URL=.' '$WORKDIR_DE/.env'"; }

check() {
  step "DE"
  bot_facts
  say "bot folder: $WORKDIR_DE"
  say "bot: $(bot_health)"
  if bot_is_remote; then
    say "database: ALREADY on the gateway (DATABASE_URL is set)"
  else
    say "database: local file, $(de "du -h '$WORKDIR_DE/data/bot.db' | cut -f1")"
  fi
  if de "docker exec $CONTAINER python -c 'import db.remote' >/dev/null 2>&1"; then
    say "bot release: knows the gateway"
  else
    say "bot release: TOO OLD for the gateway. First, on DE:"
    say "  bash $DE_SRC/scripts/update_bot.sh $DE_SRC --apply"
  fi
  de "bash $DE_SRC/scripts/db_tunnel.sh --status" || true
  step "RU"
  ru "df -h --output=avail / | tail -1 | sed 's/^ */free disk: /'; free -m | awk '/^Mem:/ {print \"memory: \" \$7 \" MB available of \" \$2}'" \
    || stop "RU does not answer"
  if ru "test -f $RU_SRC/scripts/db_gateway_install.sh"; then
    ru "bash $RU_SRC/scripts/db_tunnel.sh --status; bash $RU_SRC/scripts/db_gateway_install.sh --status" || true
  else
    say "code: not copied yet (prepare does it)"
  fi
  say ""
  say "CUTOVER_CHECK_DONE"
}

copy_code() {
  step "Copy the gateway's code from DE to RU"
  de "tar -C $DE_SRC --exclude=__pycache__ -czf - db logging_setup.py deploy/db-gateway \
      scripts/db_gateway_install.sh scripts/db_tunnel.sh scripts/db_tool.py scripts/ru_firewall.sh \
      scripts/retention_archiver.py scripts/ru_integrity.sh scripts/retire_payment_edge.sh" \
    | ru "rm -rf $RU_SRC.new && mkdir -p $RU_SRC.new && tar -C $RU_SRC.new -xzf - \
          && rm -rf $RU_SRC && mv $RU_SRC.new $RU_SRC" \
    || stop "the copy did not arrive (is $DE_SRC up to date on DE?)"
  ru "test -s $RU_SRC/db/gateway.py && test -s $RU_SRC/scripts/db_gateway_install.sh" \
    || stop "the copy on RU is incomplete"
  say "copied to RU:$RU_SRC"
}

public_key() {
  # $1: de or ru. Makes that side's key if needed; only the public half comes back.
  local out key
  if [[ "$1" == de ]]; then
    out="$(de "bash $DE_SRC/scripts/db_tunnel.sh --key")" || stop "could not make the tunnel key on DE"
  else
    out="$(ru "bash $RU_SRC/scripts/db_tunnel.sh --key")" || stop "could not make the tunnel key on RU"
  fi
  key="$(awk '/^DB_TUNNEL_PUBLIC_KEY/ { print $3 }' <<<"$out")"
  [[ "$key" =~ $KEY_RE ]] || stop "no public key came back from $1"
  printf '%s' "$key"
}

firewall() {
  step "RU firewall: admit the tunnel from DE only"
  if ru "iptables -S INPUT 2>/dev/null | grep -q -- '--dport $TUNNEL_PORT ' \
        && iptables -S INPUT 2>/dev/null | grep -q -- '-i wg-db '"; then
    say "already in place"
    return
  fi
  ru "cp $RU_SRC/scripts/ru_firewall.sh $RU_FIREWALL && bash $RU_FIREWALL --apply" \
    || stop "the firewall change was not applied; nothing else was done"
  # This is a new login: exactly what --confirm asks for.
  if ! ru "bash $RU_FIREWALL --confirm"; then
    stop "could not confirm the firewall. Do nothing: in 10 minutes RU restores the old rules by itself. Then run prepare again"
  fi
}

tunnel() {
  step "Tunnel between the servers"
  local ru_key de_key n
  ru_key="$(public_key ru)"
  de_key="$(public_key de)"
  de "if ufw status 2>/dev/null | grep -q 'Status: active'; then \
        ufw allow out to $RU_PUBLIC port $TUNNEL_PORT proto udp comment 'wg-db tunnel' >/dev/null; fi" || true
  ru "bash $RU_SRC/scripts/db_tunnel.sh --up '$de_key'" || stop "the tunnel did not start on RU"
  de "bash $DE_SRC/scripts/db_tunnel.sh --up '$ru_key'" || stop "the tunnel did not start on DE"
  for n in 1 2 3 4 5 6 7 8 9 10; do
    if de "bash $DE_SRC/scripts/db_tunnel.sh --status" | grep -q '^OK '; then
      say "tunnel: up, the servers see each other"
      return
    fi
    sleep "${POLL_SECONDS:-4}"
  done
  stop "the tunnel is configured but the servers do not see each other after $n tries (udp $TUNNEL_PORT from DE to RU)"
}

archive_key() {
  # The five-year archive is sealed to the same public key as the nightly backup:
  # the private half is with the owner only, on neither server.
  step "Five-year archive on RU"
  local recipient
  recipient="$(de "sed -n 's/^AGE_RECIPIENT=//p' $BACKUP_ENV_DE 2>/dev/null | tail -1")" || true
  recipient="${recipient//[\"\'[:space:]]/}"
  if [[ "$recipient" =~ ^age1[0-9a-z]{58}$ ]]; then
    ru "bash $RU_SRC/scripts/db_gateway_install.sh --archive-key '$recipient'" \
      || say "WARN: the archive key was not set on RU; the monthly archive is off"
  else
    say "not switched on: DE has no backup key yet (docs/BACKUPS.md). Set the backup up,"
    say "then run prepare again. Until then records simply stay in the database."
  fi
}

prepare() {
  bot_facts
  de "docker exec $CONTAINER python -c 'import db.remote' >/dev/null 2>&1" \
    || stop "the bot on DE is too old for the gateway. First, on DE: bash $DE_SRC/scripts/update_bot.sh $DE_SRC --apply"
  copy_code
  firewall
  tunnel
  step "Gateway on RU (installed, not started)"
  ru "bash $RU_SRC/scripts/db_gateway_install.sh --apply" || stop "the gateway was not installed on RU"
  archive_key
  say ""
  say "CUTOVER_PREPARED: the bot still uses the file on DE; nothing has moved."
  say "Next, in a quiet hour (the bot is offline for a few minutes):  bash db_cutover.sh switch"
}

BOT_STOPPED=0
restart_bot_if_stopped() {
  if ((BOT_STOPPED)); then
    say "Starting the bot again on the file on DE..."
    de "docker start $CONTAINER >/dev/null" || say "COULD NOT START THE BOT: on DE run  docker start $CONTAINER"
    BOT_STOPPED=0
  fi
}

switch() {
  bot_facts
  ! bot_is_remote || stop "the bot already uses the gateway (DATABASE_URL is set)"
  de "bash $DE_SRC/scripts/db_tunnel.sh --status" | grep -q '^OK ' \
    || stop "the tunnel is not up: run prepare first"
  ru "test -x \${PREFIX:-}/opt/vpn-db/venv/bin/python" \
    || stop "the gateway is not installed on RU: run prepare first"
  trap restart_bot_if_stopped EXIT

  step "1/6 Stop the bot (nothing may change while the file travels)"
  de "docker stop -t 30 $CONTAINER >/dev/null" || stop "could not stop the bot"
  BOT_STOPPED=1

  step "2/6 A checked copy of the database on DE"
  de "docker run --rm --user 1000:1000 --read-only --tmpfs /tmp --network none \
        -v '$WORKDIR_DE/data:/app/data' -v '$WORKDIR_DE/secrets/db_key.txt:/run/secrets/db_key:ro' \
        '$IMAGE_DE' python scripts/db_tool.py backup /app/data/bot.db /app/data/move-to-ru.db" \
    || stop "the copy could not be made or failed its check"
  local sum_de sum_ru key_len
  sum_de="$(de "sha256sum '$WORKDIR_DE/data/move-to-ru.db' | cut -d' ' -f1")"
  [[ "$sum_de" =~ ^[0-9a-f]{64}$ ]] || stop "could not read the copy's checksum on DE"

  step "3/6 Carry the database and its key to RU"
  ru "bash $RU_SRC/scripts/db_gateway_install.sh --stop >/dev/null" || true
  sum_ru="$(de "cat '$WORKDIR_DE/data/move-to-ru.db'" \
    | ru "umask 077; cat > $RU_DATA/bot.db.incoming && sha256sum $RU_DATA/bot.db.incoming | cut -d' ' -f1")" \
    || stop "the database did not arrive on RU"
  [[ "$sum_ru" == "$sum_de" ]] || stop "the file changed on the way (checksums differ); nothing was switched"
  say "database: arrived intact"
  key_len="$(de "cat '$WORKDIR_DE/secrets/db_key.txt'" \
    | ru "umask 077; tr -d '\r\n ' > $RU_SECRETS/db_key && wc -c < $RU_SECRETS/db_key")" \
    || stop "the key did not arrive on RU"
  [[ "${key_len//[[:space:]]/}" == 64 ]] || stop "the key on RU is not 64 characters"
  say "key: arrived"

  step "4/6 Start the gateway on RU"
  ru "cd $RU_DATA && if [ -s bot.db ]; then mv bot.db bot.db.before-$STAMP; fi \
        && rm -f bot.db-wal bot.db-shm && mv bot.db.incoming bot.db" \
    || stop "could not put the file in place on RU"
  ru "bash $RU_SRC/scripts/db_gateway_install.sh --start" || stop "the gateway did not start on RU"

  step "5/6 Point the bot at the gateway"
  if ! ru "cat $RU_SECRETS/db_gateway_token" \
    | de "bash $DE_SRC/scripts/update_bot.sh --database-gateway $GATEWAY_URL"; then
    # update_bot.sh put the old settings back if it got as far as changing them.
    if [[ "$(bot_health)" == healthy ]]; then
      BOT_STOPPED=0
    fi
    ru "bash $RU_SRC/scripts/db_gateway_install.sh --stop >/dev/null" || true
    stop "the bot did not come up on the gateway. It is back on the file on DE; the gateway on RU is stopped"
  fi
  BOT_STOPPED=0

  step "6/6 Check"
  bot_is_remote || stop "the bot is running, but DATABASE_URL is not set: look at $WORKDIR_DE/.env"
  ru "bash $RU_SRC/scripts/db_gateway_install.sh --status"
  de "rm -f '$WORKDIR_DE/data/move-to-ru.db'" || true
  trap - EXIT
  say ""
  say "CUTOVER_DONE: the bot's data now lives on RU. The file on DE is an old copy."
  say "Open the bot and press /start. To go back today:  bash db_cutover.sh rollback"
}

rollback() {
  bot_facts
  bot_is_remote || stop "the bot already uses the file on DE"
  step "Bot back on the file on DE"
  de "bash $DE_SRC/scripts/update_bot.sh --database-local" || stop "the bot did not go back; see the message above"
  ru "bash $RU_SRC/scripts/db_gateway_install.sh --stop" || true
  say ""
  say "CUTOVER_ROLLED_BACK. Records made while the bot used RU are NOT in the file on DE."
}

update() {
  # The gateway applies schema changes and must know every method the new bot
  # calls, so it is always updated first.
  copy_code
  step "Gateway on RU: new code"
  ru "bash $RU_SRC/scripts/db_gateway_install.sh --apply" || stop "the gateway was not updated on RU"
  archive_key
  ru "bash $RU_SRC/scripts/db_gateway_install.sh --status"
  say ""
  say "CUTOVER_GATEWAY_UPDATED. Now the bot, on DE:  bash $DE_SRC/scripts/update_bot.sh $DE_SRC --apply"
}

connection_log() {
  local want="${1:-}" n
  [[ "$want" == on || "$want" == off ]] || stop "usage: db_cutover.sh connection-log on|off"
  bot_facts
  if [[ "$want" == on ]]; then
    # People are told before it starts: the policy the bot publishes must already
    # describe these records. The check is piped in, as update_bot.sh --doctor does;
    # what it reads is the policy inside the running bot's image.
    de "docker exec -i $CONTAINER python3 - --policy-keeps-connection-records < $DE_SRC/scripts/doctor_bot.py" \
      || stop "the policy the bot publishes does not describe connection records yet. Update the bot first: bash $DE_SRC/scripts/update_bot.sh $DE_SRC --apply"
  fi
  if bot_is_remote; then
    step "Connection records $want (on RU, next to the data)"
    ru "bash $RU_SRC/scripts/db_gateway_install.sh --connection-log $want" \
      || stop "the switch was not changed on RU"
    # The bot reads the switch when it connects to the gateway.
    de "docker restart $CONTAINER >/dev/null" || stop "the switch is set, but the bot did not restart"
    for n in 1 2 3 4 5 6 7 8 9 10 11 12; do
      [[ "$(bot_health)" == healthy ]] && break
      sleep "${POLL_SECONDS:-10}"
    done
    [[ "$(bot_health)" == healthy ]] || stop "the switch is set, but the bot is not healthy after $n checks"
  else
    step "Connection records $want (the database is still on DE)"
    if [[ "$want" == on ]]; then
      de "bash $DE_SRC/scripts/update_bot.sh --set ACTIVITY_LOGGING_ENABLED=true" || stop "the switch was not changed"
    else
      de "bash $DE_SRC/scripts/update_bot.sh --set ACTIVITY_LOGGING_ENABLED=false" || stop "the switch was not changed"
    fi
  fi
  say ""
  say "CUTOVER_CONNECTION_LOG $want"
}

status() {
  bot_facts
  step "DE"
  say "bot: $(bot_health)"
  if bot_is_remote; then
    say "database: on RU, through the gateway"
  else
    say "database: the file on DE"
  fi
  de "bash $DE_SRC/scripts/db_tunnel.sh --status" || true
  step "RU"
  ru "bash $RU_SRC/scripts/db_gateway_install.sh --status; bash $RU_FIREWALL --status; \
      bash $RU_SRC/scripts/ru_integrity.sh --status; \
      df -h --output=avail / | tail -1 | sed 's/^ */free disk: /'" || say "RU does not answer"
}

case "${1:-}" in
  check) check ;;
  prepare) prepare ;;
  switch) switch ;;
  rollback) rollback ;;
  status) status ;;
  update) update ;;
  connection-log) connection_log "${2:-}" ;;
  *) stop "usage: db_cutover.sh check | prepare | switch | rollback | status | update | connection-log on|off" ;;
esac
