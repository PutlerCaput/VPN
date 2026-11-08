#!/usr/bin/env bash
# Safe update of the DE server: system packages (Docker included), the base
# images, the bot and the site, and a check of both databases.
#
#   bash scripts/server_update.sh            # report only: changes nothing
#   bash scripts/server_update.sh --apply    # update, step by step
#
# With --apply, in this order, stopping at the first step that fails:
#   1. copies: Marzban's database (online copy, integrity-checked), the
#      Marzban and bot settings; the newest three of each are kept;
#   2. system packages: upgrade that never removes a package and keeps every
#      changed config file; Docker's previous versions are saved for a
#      downgrade; containers that ran before are checked to run again;
#   3. fresh base images, then the bot and the site rebuilt through
#      update_bot.sh and update_web.sh, which keep a rollback image and switch
#      back by themselves if the new one is not healthy;
#   4. cleanup (server_cleanup.sh --apply) and the doctor, before and after.
#
# Never touched: Marzban's version and settings, Xray, its rule and geo files,
# the firewall, SSH. A new kernel waits for a reboot at a time you choose.
set -o pipefail

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1
[[ -z "${1:-}" || "$APPLY" == 1 ]] || { echo "usage: server_update.sh [--apply]" >&2; exit 2; }

SRC="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
CONTAINER="${CONTAINER:-node-rental-bot-1}"
BACKUP_DIR="${BACKUP_DIR:-/root/server-backups}"
MARZBAN_DB="${MARZBAN_DB:-/var/lib/marzban/db.sqlite3}"
MARZBAN_DIR="${MARZBAN_DIR:-/opt/marzban}"
KEEP=3
MIN_FREE_GB=3
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
DOCKER_PACKAGES=(docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin)

say() { printf '%s\n' "$*"; }
section() { printf '\n== %s\n' "$*"; }
stop() { printf 'SERVER_UPDATE_STOPPED: %s\n' "$*" >&2; exit 1; }

free_gb() { df -P --block-size=1G / | awk 'NR == 2 { print $4 }'; }

keep_newest() {  # keep_newest <glob pattern inside BACKUP_DIR>
  local n=0 file
  while IFS= read -r file; do
    n=$((n + 1))
    ((n > KEEP)) && rm -f -- "$file"
  done < <(find "$BACKUP_DIR" -maxdepth 1 -type f -name "$1" -printf '%T@ %p\n' | sort -rn | cut -d' ' -f2-)
}

doctor() {  # prints the doctor's last line, returns its status
  local out status
  out="$(bash "$SRC/scripts/update_bot.sh" --doctor 2>&1)"
  status=$?
  printf '%s\n' "$out" | grep -E '^(FAIL|WARN)' | sed 's/^/  /'
  printf '%s\n' "$out" | tail -n 1 | sed 's/^/  /'
  return "$status"
}

running_containers() { docker ps --format '{{.Names}}' | sort; }

wait_containers() {  # wait_containers <names...>: until each runs (and is healthy if it can be)
  local deadline=$((SECONDS + 240)) name state missing
  while ((SECONDS < deadline)); do
    missing=()
    for name in "$@"; do
      state="$(docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}' "$name" 2>/dev/null)"
      [[ "$state" == "running " || "$state" == "running healthy" ]] || missing+=("$name")
    done
    ((${#missing[@]} == 0)) && return 0
    sleep 5
  done
  for name in "${missing[@]}"; do
    say "  not back: $name ($(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null || echo missing))"
  done
  return 1
}

# A dropped connection (a phone on a mobile network, say) would stop --apply
# half-way, possibly in the middle of a package upgrade. Inside tmux or screen
# the update keeps running and the session can be reopened.
if ((APPLY)) && [[ -n "${SSH_CONNECTION:-}" && -z "${TMUX:-}" && -z "${STY:-}" \
  && "${SERVER_UPDATE_DIRECT:-0}" != 1 ]]; then
  cat >&2 <<EOF
SERVER_UPDATE_NOT_STARTED: you are connected over SSH. If the connection drops,
the update stops half-way. Run it inside tmux, which keeps going:

  command -v tmux >/dev/null || apt-get install -y tmux
  tmux new -s update
  bash $SRC/scripts/server_update.sh --apply

Connection lost? Log in again and run: tmux attach -t update
To run it directly anyway: SERVER_UPDATE_DIRECT=1 bash $0 --apply
EOF
  exit 2
fi

[[ $EUID -eq 0 ]] || stop "run as root"
[[ -f "$SRC/Dockerfile" && -f "$SRC/scripts/update_bot.sh" ]] || stop "run from the project checkout"
command -v docker >/dev/null 2>&1 || stop "docker not found"

if ((APPLY)); then
  say "== APPLY: the server is updated"
else
  say "== REPORT ONLY: refreshes the package lists, changes nothing else"
fi
say "source: $SRC $(git -C "$SRC" log -1 --format='%h %s' 2>/dev/null)"
say "time: $STAMP"

section "System"
say "  $( (. /etc/os-release 2>/dev/null && printf '%s' "$PRETTY_NAME") ), kernel $(uname -r)"
say "  free on /: $(free_gb) GB; memory: $(free -m | awk 'NR == 2 { print $7 " MB available of " $2 }')"
say "  docker: $(docker version -f '{{.Server.Version}}' 2>/dev/null || echo 'not running')"
[[ -f /var/run/reboot-required ]] && say "  REBOOT NEEDED: $(tr '\n' ' ' < /var/run/reboot-required.pkgs 2>/dev/null)"

section "Doctor before"
doctor
doctor_before=$?

section "Disk"
if (($(free_gb) < MIN_FREE_GB)); then
  if ((APPLY)); then
    say "  less than $MIN_FREE_GB GB free: cleaning up first"
    bash "$SRC/scripts/server_cleanup.sh" --apply >/dev/null 2>&1
    (($(free_gb) >= MIN_FREE_GB)) || stop "only $(free_gb) GB free on /; free space first"
  else
    say "  less than $MIN_FREE_GB GB free: --apply cleans up first"
  fi
else
  say "  enough space"
fi

# ---------------------------------------------------------------- copies
section "Copies"
if ((APPLY)); then
  umask 077
  mkdir -p "$BACKUP_DIR"
  if [[ -f "$MARZBAN_DB" ]]; then
    copy="$BACKUP_DIR/marzban-db-$STAMP.sqlite3"
    # SQLite's online backup: a consistent copy while Marzban keeps running.
    python3 - "$MARZBAN_DB" "$copy" <<'PY' || stop "Marzban database copy failed"
import sqlite3
import sys

try:
    source = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=30)
    source.execute("SELECT 1 FROM sqlite_master LIMIT 1")
except sqlite3.OperationalError:
    source = sqlite3.connect(sys.argv[1], timeout=30)  # backup() only reads
target = sqlite3.connect(sys.argv[2])
with target:
    source.backup(target)
check = target.execute("PRAGMA integrity_check").fetchone()[0]
target.close()
source.close()
if check != "ok":
    sys.exit(f"integrity_check: {check}")
PY
    say "  Marzban database: $copy (integrity ok)"
    keep_newest 'marzban-db-*.sqlite3'
  else
    say "  Marzban database not at $MARZBAN_DB (MARZBAN_DB=... to point at it): skipped"
  fi
  settings=()
  if [[ -d "$MARZBAN_DIR" ]]; then
    if (($(du -sm "$MARZBAN_DIR" | cut -f1) <= 200)); then
      settings+=("$MARZBAN_DIR")
    else
      say "  $MARZBAN_DIR is larger than 200 MB: not copied"
    fi
  fi
  [[ -f /var/lib/marzban/xray_config.json ]] && settings+=(/var/lib/marzban/xray_config.json)
  workdir="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$CONTAINER" 2>/dev/null)"
  if [[ -n "$workdir" && -d "$workdir" ]]; then
    [[ -f "$workdir/.env" ]] && settings+=("$workdir/.env")
    [[ -d "$workdir/secrets" ]] && settings+=("$workdir/secrets")
  fi
  if ((${#settings[@]})); then
    tar -czf "$BACKUP_DIR/settings-$STAMP.tar.gz" --exclude='*.sqlite3' "${settings[@]}" 2>/dev/null \
      || stop "settings copy failed"
    say "  settings: $BACKUP_DIR/settings-$STAMP.tar.gz (${settings[*]})"
    keep_newest 'settings-*.tar.gz'
  fi
  say "  the bot's own database copy is made by update_bot.sh below"
else
  say "  would copy: $MARZBAN_DB (online, checked), $MARZBAN_DIR, xray_config.json, the bot's .env and secrets"
  say "  into $BACKUP_DIR, keeping the newest $KEEP of each"
fi

# -------------------------------------------------------------- packages
section "System packages"
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l
  apt-get update -qq >/dev/null 2>&1 || say "  package lists could not be refreshed"
  mapfile -t upgradable < <(apt list --upgradable 2>/dev/null | grep -v '^Listing' | cut -d/ -f1)
  say "  packages to upgrade: ${#upgradable[@]}"
  ((${#upgradable[@]})) && printf '    %s\n' "${upgradable[@]}" | head -n 40
  for pkg in "${DOCKER_PACKAGES[@]}"; do
    installed="$(dpkg-query -W -f='${Version}' "$pkg" 2>/dev/null)"
    [[ -n "$installed" ]] || continue
    candidate="$(apt-cache policy "$pkg" 2>/dev/null | awk '/Candidate:/ { print $2 }')"
    say "  $pkg: $installed$([[ "$candidate" != "$installed" ]] && printf ' -> %s' "$candidate")"
  done
  if ((APPLY && ${#upgradable[@]})); then
    mkdir -p "$BACKUP_DIR"
    versions="$BACKUP_DIR/docker-versions-$STAMP.txt"
    dpkg-query -W -f='${Package}=${Version}\n' "${DOCKER_PACKAGES[@]}" 2>/dev/null > "$versions"
    keep_newest 'docker-versions-*.txt'
    mapfile -t before < <(running_containers)
    say "  upgrading (nothing is removed, changed configs are kept)..."
    apt-get -y -q -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold \
      --with-new-pkgs upgrade > "$BACKUP_DIR/apt-$STAMP.log" 2>&1 \
      || stop "apt upgrade failed: see $BACKUP_DIR/apt-$STAMP.log"
    keep_newest 'apt-*.log'
    say "  done; Docker's previous versions: $versions"
    say "  downgrade if ever needed: apt-get install --allow-downgrades $(tr '\n' ' ' < "$versions")"
    if ((${#before[@]})); then
      for name in "${before[@]}"; do
        # A container without a restart policy does not come back by itself.
        [[ "$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null)" == true ]] || docker start "$name" >/dev/null 2>&1
      done
      if wait_containers "${before[@]}"; then
        say "  all ${#before[@]} containers run again"
      else
        stop "containers above did not come back; see: docker ps -a"
      fi
    fi
  fi
  [[ -f /var/run/reboot-required ]] && say "  a reboot is needed for the new kernel or libraries; choose a quiet hour: reboot"
else
  say "  apt-get not found: skipped"
fi

# ---------------------------------------------------------- images, bot, site
section "Images"
mapfile -t bases < <(awk 'toupper($1) == "FROM" { print $2 }' "$SRC/Dockerfile" | sort -u)
say "  base images of the bot and the site: ${bases[*]}"
say "  images of other containers (updated by hand, never by this script):"
docker ps --format '{{.Names}}\t{{.Image}}' | grep -v '^node-rental-' | sed 's/^/    /'
if ((APPLY)); then
  for image in "${bases[@]}"; do
    if docker pull -q "$image" >/dev/null 2>&1; then
      say "  pulled: $image"
    else
      say "  kept the local $image (pull failed)"
    fi
  done
  if docker inspect "$CONTAINER" >/dev/null 2>&1; then
    section "Bot"
    out="$(bash "$SRC/scripts/update_bot.sh" "$SRC" --apply 2>&1)"
    printf '%s\n' "$out" | tail -n 4 | sed 's/^/  /'
    grep -q 'UPDATE_OK' <<<"$out" || stop "the bot update did not finish; it rolled back by itself"
    section "Site"
    out="$(bash "$SRC/scripts/update_web.sh" "$SRC" --apply 2>&1)"
    printf '%s\n' "$out" | tail -n 4 | sed 's/^/  /'
    grep -q 'WEB_UPDATE_OK' <<<"$out" || stop "the site update did not finish; it rolled back by itself"
  else
    say "  bot container $CONTAINER not here: bot and site skipped"
  fi
else
  say "  --apply pulls the base images and rebuilds the bot and the site with them"
fi

# ---------------------------------------------------------------- databases
section "Databases"
if docker inspect "$CONTAINER" >/dev/null 2>&1; then
  if docker exec "$CONTAINER" python scripts/db_tool.py check /app/data/bot.db >/dev/null 2>&1; then
    say "  bot database: check ok"
  else
    say "  bot database: CHECK FAILED; see: docker exec $CONTAINER python scripts/db_tool.py check /app/data/bot.db"
  fi
fi
if [[ -f "$MARZBAN_DB" ]]; then
  say "  Marzban database: $(du -h "$MARZBAN_DB" | cut -f1)$( ((APPLY)) && printf ', copy checked above')"
fi
for asset in /var/lib/marzban/assets/geosite.dat /var/lib/marzban/assets/geoip.dat; do
  [[ -f "$asset" ]] && say "  $(basename "$asset"): $(( ($(date +%s) - $(stat -c %Y "$asset")) / 86400 )) days old (update: docs/SECURITY_POLICY.md)"
done

# ---------------------------------------------------------------- traffic
section "Server traffic (network card)"
iface="$(ip -o route show default 2>/dev/null | awk '{ print $5; exit }')"
if command -v vnstat >/dev/null 2>&1 && [[ -n "$iface" ]]; then
  vnstat -m -i "$iface" 2>/dev/null | tail -n 6 | sed 's/^/  /'
  say "  compare with the bot's /traffic and the hosting panel"
elif ((APPLY)) && command -v apt-get >/dev/null 2>&1; then
  if apt-get install -y -q vnstat >/dev/null 2>&1; then
    say "  vnstat installed: it counts $iface traffic from now on"
  else
    say "  vnstat could not be installed"
  fi
else
  say "  vnstat is not installed; --apply installs it to count the traffic of ${iface:-the main interface}"
fi

# ---------------------------------------------------------------- cleanup
section "Cleanup"
if ((APPLY)); then
  bash "$SRC/scripts/server_cleanup.sh" --apply 2>&1 | tail -n 25 | sed 's/^/  /'
else
  say "  report: bash scripts/server_cleanup.sh"
fi

section "Doctor after"
if ((APPLY)); then
  doctor
  doctor_after=$?
  say ""
  if ((doctor_after != 0 && doctor_before == 0)); then
    say "SERVER_UPDATE_DONE_WITH_NEW_PROBLEMS: see the doctor above. Rollback images:"
    say "  docker images node-rental-bot; docker images node-rental-web"
    exit 1
  fi
  say "SERVER_UPDATE_OK"
else
  say "  (runs with --apply)"
  say ""
  say "REPORT DONE. To update: bash scripts/server_update.sh --apply"
fi
