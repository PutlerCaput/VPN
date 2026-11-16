#!/usr/bin/env bash
# Server cleanup: old Docker images and build cache, old backups and copies of
# secrets, leftover delivery folders, oversized container logs, old kernels,
# system journal, and technical rows of the bot database.
#
#   bash scripts/server_cleanup.sh                   # report only: removes nothing
#   bash scripts/server_cleanup.sh --apply           # removes what the report lists
#   bash scripts/server_cleanup.sh --auto            # the weekly run (scripts/install_cleanup.sh)
#   bash scripts/server_cleanup.sh --auto --dry-run  # what the weekly run would do now
#
# Never removed: running images, the newest two rollback images of each service,
# :latest, the base images of the bot and the site, payments and legal records,
# the GitHub checkout, the encrypted copies made by backup.sh, the running
# kernel. The build cache is cut down to the layers of the latest builds (2 GB),
# so the next build is not slow. The database loses only technical rows
# (db/maintenance.py) and gets a checked copy first. Stopped containers, volumes, images no container uses
# and large files are only listed: whether they are needed is your call.
#
# --auto is the same cleanup cut down to what is safe when nobody is watching:
#   * it does nothing while an update, a backup or the package manager runs,
#     while another cleanup runs, or while any container is not healthy;
#   * it removes only what an update has already replaced, and nothing younger
#     than MIN_AGE_DAYS (7): old rollback and update images, dangling images,
#     build cache above 2 GB that was not used for 3 days, copies of the
#     database and .env, the system journal, oversized container logs;
#   * it never touches kernels, the bot database, other versions of images,
#     old keys, delivery folders, stopped containers or volumes: those are
#     listed, and a supervised "--apply" (or server_update.sh) handles them;
#   * afterwards it checks that the same containers still run, records the run
#     for the doctor (scripts/cleanup_status.sh) and tells the owner in Telegram
#     (NOTIFY=always|problems|never).
set -o pipefail

usage() {
  echo "usage: server_cleanup.sh [--apply | --auto [--dry-run]]" >&2
  exit 2
}
APPLY=0
AUTO=0
DRY=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --auto) AUTO=1 ;;
    --dry-run) DRY=1 ;;
    *) usage ;;
  esac
done
((AUTO && APPLY)) && usage
((DRY && !AUTO)) && usage
((AUTO && !DRY)) && APPLY=1

CONTAINER="${CONTAINER:-node-rental-bot-1}"
KEEP_ROLLBACKS=2
KEEP_COPIES=2
CACHE_KEEP="${CACHE_KEEP:-2GB}"  # build cache that stays: the layers of the latest builds
MB=$((1024 * 1024))
LOG_REPORT_MB=50
LOG_EMPTY_MB=200
HOME_DIR="${CLEANUP_HOME:-/root}"
# Settings of the weekly run. They come from the systemd unit, so each one is
# checked here: a typo must never widen what is removed.
MIN_AGE_DAYS="${MIN_AGE_DAYS:-7}"
CACHE_MIN_AGE_HOURS="${CACHE_MIN_AGE_HOURS:-72}"
LOW_FREE_MB="${LOW_FREE_MB:-3072}"
[[ "$MIN_AGE_DAYS" =~ ^[0-9]+$ ]] || MIN_AGE_DAYS=7
((MIN_AGE_DAYS >= 1)) || MIN_AGE_DAYS=7
[[ "$CACHE_MIN_AGE_HOURS" =~ ^[0-9]+$ ]] || CACHE_MIN_AGE_HOURS=72
[[ "$LOW_FREE_MB" =~ ^[0-9]+$ ]] || LOW_FREE_MB=3072
STATE_DIR="${STATE_DIR:-/var/lib/node-cleanup}"
LOCK_FILE="${LOCK_FILE:-/run/lock/node-cleanup.lock}"
NOTIFY="${NOTIFY:-always}"
case "$NOTIFY" in always | problems | never) ;; *) NOTIFY=always ;; esac
# A maintenance script of this project, started with bash or directly.
BUSY_PATTERN='bash +[^ ]*(server_update|server_auto_update|update_bot|update_web|backup_all|restore_backup|ssh_hardening)\.sh'
SRC="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
NOW="$(date +%s)"
removed_n=0
freed=0
free_before=0
after_free=0
manual_n=0

say() { printf '%s\n' "$*"; }
free_mb() { df -Pm / 2>/dev/null | awk 'NR == 2 { print $4 }'; }
running_containers() { docker ps --format '{{.Names}}' 2>/dev/null | sort | tr '\n' ' ' | sed 's/ *$//'; }
# Running containers that restart, report "unhealthy" or have not finished starting.
unhealthy_containers() {
  docker ps --format '{{.Names}}|{{.Status}}' 2>/dev/null \
    | awk -F'|' '$2 ~ /unhealthy|health: starting|Restarting/ { printf "%s ", $1 }' | sed 's/ *$//'
}

# What the doctor reads (scripts/cleanup_status.sh).
state_write() {  # state_write <ok|skipped|failed> <note>
  ((AUTO && !DRY)) || return 0
  local note="${2//$'\n'/ }" last_ok now
  mkdir -p "$STATE_DIR" 2>/dev/null || return 0
  # A skipped run keeps the time of the last run that finished.
  now="$(date +%s)"
  last_ok="$(sed -n 's/^last_ok=//p' "$STATE_DIR/last-run" 2>/dev/null | head -n1)"
  [[ "$1" == ok ]] && last_ok="$now"
  printf 'time=%s\nlast_ok=%s\nresult=%s\nfreed_mb=%s\nfree_mb=%s\nmanual=%s\nnote=%s\n' \
    "$now" "$last_ok" "$1" "$freed" "$after_free" "$manual_n" "$note" >"$STATE_DIR/last-run.tmp" 2>/dev/null \
    && mv -f "$STATE_DIR/last-run.tmp" "$STATE_DIR/last-run" 2>/dev/null
  return 0
}
# A short note to the owner from the bot, best effort.
notify() {  # notify <always|problem> <text>
  ((AUTO && !DRY)) || return 0
  [[ "$NOTIFY" == never ]] && return 0
  [[ "$1" == always && "$NOTIFY" != always ]] && return 0
  docker exec "$CONTAINER" python scripts/send_backup.py text "$2" >/dev/null 2>&1 || true
}
# Nothing is wrong, the next run simply tries again.
auto_skip() {
  say "CLEANUP_AUTO_SKIPPED: $1"
  state_write skipped "$1"
  exit 0
}
auto_fail() {  # auto_fail <reason> [<text for the owner>]
  printf 'CLEANUP_AUTO_FAILED: %s\n' "$1" >&2
  state_write failed "$1"
  notify problem "⚠️ ${2:-Автоочистка сервера остановилась: $1}"
  exit 1
}

remove() {  # remove <path>...
  local path
  for path in "$@"; do
    if ((APPLY)); then
      if rm -rf -- "$path"; then
        removed_n=$((removed_n + 1))
        say "  removed: $path"
      else
        say "  FAILED: $path"
      fi
    else
      say "  would remove: $path"
    fi
  done
}

# The newest copies stay. The weekly run also keeps every copy younger than
# MIN_AGE_DAYS, so the copy made before a recent update is still there.
prune_newest() {  # prune_newest <directory> <name pattern> <how many to keep>
  local dir="$1" pattern="$2" keep="$3" n=0 line file mtime
  [[ -d "$dir" ]] || return 0
  while IFS= read -r line; do
    file="${line#* }"
    mtime="${line%% *}"
    n=$((n + 1))
    if ((n <= keep)); then
      say "  keep: $file"
    elif ((AUTO)) && ((NOW - ${mtime%.*} < MIN_AGE_DAYS * 86400)); then
      say "  keep: $file (younger than $MIN_AGE_DAYS days)"
    else
      remove "$file"
    fi
  done < <(find "$dir" -maxdepth 1 -type f -name "$pattern" -printf '%T@ %p\n' 2>/dev/null | sort -rn)
}

# The time in a tag such as rollback-20261109T144339Z-135911; nothing if it has none.
stamp_epoch() {
  local stamp
  stamp="$(printf '%s' "$1" | grep -oE '[0-9]{8}T[0-9]{6}Z' | head -n1)"
  [[ -n "$stamp" ]] || return 0
  date -u -d "${stamp:0:4}-${stamp:4:2}-${stamp:6:2} ${stamp:9:2}:${stamp:11:2}:${stamp:13:2}" +%s 2>/dev/null
}

if ((AUTO)) && ! docker info >/dev/null 2>&1; then
  auto_skip "docker is not answering"
fi

# Names the containers were created with: update_web.sh reads them as its
# current image, so an image named by any container must keep its tag.
mapfile -t in_use < <(docker ps -a --format '{{.Image}}' | sort -u)

WORKDIR="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$CONTAINER" 2>/dev/null)"
if [[ -z "$WORKDIR" || ! -d "$WORKDIR" ]]; then
  if ((AUTO)); then
    auto_fail "bot container $CONTAINER not found" "Автоочистка не запустилась: не найден контейнер бота"
  fi
  say "bot container $CONTAINER not found: nothing done"
  exit 1
fi

if ((AUTO)); then
  # Several things can already be going on; any of them ends this run quietly.
  mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null
  command -v flock >/dev/null 2>&1 || auto_fail "flock is missing (apt-get install util-linux)"
  command -v pgrep >/dev/null 2>&1 || auto_fail "pgrep is missing (apt-get install procps)"
  exec 9>"$LOCK_FILE" || auto_fail "the lock file $LOCK_FILE cannot be opened"
  flock -n 9 || auto_skip "another cleanup is still running"
  if pgrep -f "$BUSY_PATTERN" >/dev/null 2>&1; then
    auto_skip "an update, a backup or another maintenance script is running"
  fi
  # Only real package operations count. unattended-upgrade-shutdown runs all the
  # time on Ubuntu and shares the process name "unattended-upgr" with an upgrade
  # in progress (that one is bin/unattended-upgrade): matching the name made every
  # weekly run skip.
  if pgrep -x 'apt|apt-get|dpkg' >/dev/null 2>&1 \
    || pgrep -f '(^|/)unattended-upgrade( |$)' >/dev/null 2>&1; then
    auto_skip "the package manager is running"
  fi
  troubled="$(unhealthy_containers)"
  [[ -z "$troubled" ]] || auto_skip "not touching anything while a container is not healthy: $troubled"
  running_before="$(running_containers)"
  [[ " $running_before " == *" $CONTAINER "* ]] || auto_skip "the bot container is not running"
  free_before="$(free_mb)"
  free_before="${free_before:-0}"
fi

if ((AUTO && DRY)); then
  say "== AUTO DRY RUN: what the weekly run would do; nothing is removed"
elif ((AUTO)); then
  say "== AUTO: the weekly run removes only the safe items below"
elif ((APPLY)); then
  say "== APPLY: the items below are removed"
else
  say "== REPORT ONLY: nothing is removed"
fi

say ""
say "== Disk"
df -h / | sed -n '2p'
say "== Docker"
docker system df 2>/dev/null
say "== Project folders"
du -sh "$WORKDIR/data" "$WORKDIR/logs" "$WORKDIR/backups" 2>/dev/null

say ""
say "== Docker images of this project"
for repo in node-rental-bot node-rental-web; do
  mapfile -t tags < <(docker images --format '{{.Repository}}:{{.Tag}}' "$repo" | grep -v '<none>' | sort)
  mapfile -t keep_rollbacks < <(printf '%s\n' "${tags[@]}" | grep ':rollback-' | tail -n "$KEEP_ROLLBACKS")
  for tag in "${tags[@]}"; do
    if [[ "$tag" == "$repo:latest" ]]; then
      say "  keep: $tag"
      continue
    fi
    kept=0
    for name in "${keep_rollbacks[@]}" "${in_use[@]}"; do
      [[ "$tag" == "$name" ]] && kept=1
    done
    young=0
    if ((AUTO)); then
      # The tag carries the time of the update. A tag without one is kept.
      stamp="$(stamp_epoch "$tag")"
      if [[ -z "$stamp" ]] || ((NOW - stamp < MIN_AGE_DAYS * 86400)); then young=1; fi
    fi
    if ((kept)); then
      say "  keep: $tag"
    elif ((young)); then
      say "  keep: $tag (younger than $MIN_AGE_DAYS days, or its age is unknown)"
    elif ((APPLY)); then
      # No -f: an image used by a running or stopped container is refused.
      if docker rmi "$tag" >/dev/null 2>&1; then
        removed_n=$((removed_n + 1))
        say "  removed: $tag"
      else
        say "  kept (in use): $tag"
      fi
    else
      say "  would remove: $tag"
    fi
  done
done
if ((APPLY)); then
  # The weekly run leaves alone what is younger than MIN_AGE_DAYS (images) or
  # was used within CACHE_MIN_AGE_HOURS (build cache).
  dangling_filter=()
  cache_filter=()
  if ((AUTO)); then
    dangling_filter=(--filter "until=$((MIN_AGE_DAYS * 24))h")
    cache_filter=(--filter "until=${CACHE_MIN_AGE_HOURS}h")
  fi
  if docker image prune -f "${dangling_filter[@]}" >/dev/null 2>&1; then
    if ((AUTO)); then say "  dangling images older than $MIN_AGE_DAYS days removed"; else say "  dangling images removed"; fi
  fi
  # An old week-long rule left 5 GB of cache on a 15 GB disk, because every
  # build is recent. A cap keeps the latest layers and drops the rest. The flag
  # was --keep-storage before Docker 28; the last resort is the age filter.
  cache_done=0
  for flag in --reserved-space --keep-storage; do
    if docker builder prune -f "$flag" "$CACHE_KEEP" "${cache_filter[@]}" >/dev/null 2>&1; then
      say "  build cache cut down to $CACHE_KEEP: the layers of the latest builds stay"
      cache_done=1
      break
    fi
  done
  if ((!cache_done)); then
    if ((AUTO)); then
      if docker builder prune -f --filter "until=${CACHE_MIN_AGE_HOURS}h" >/dev/null 2>&1; then
        say "  build cache not used for $CACHE_MIN_AGE_HOURS hours removed"
      fi
    elif docker builder prune -f --filter until=24h >/dev/null 2>&1; then
      say "  build cache older than a day removed"
    fi
  fi
elif ((AUTO)); then
  say "  would remove: dangling images older than $MIN_AGE_DAYS days; build cache above $CACHE_KEEP that was not used for $CACHE_MIN_AGE_HOURS hours"
else
  say "  would remove: dangling images; build cache above $CACHE_KEEP (the layers of the latest builds stay)"
fi

say ""
say "== Other versions of running services' images (re-downloaded if ever needed)"
mapfile -t bases < <(awk 'toupper($1) == "FROM" { print $2 }' "$SRC/Dockerfile" 2>/dev/null | sort -u)
# A container started from "amnezia-awg2" runs "amnezia-awg2:latest", so both
# sides are compared with the tag spelled out.
norm() { local r="$1"; [[ "${r##*/}" == *:* ]] || r="$r:latest"; printf '%s' "$r"; }
declare -A used_image=() used_repo=()
for ref in "${in_use[@]}"; do
  ref="$(norm "$ref")"
  used_image["$ref"]=1
  used_repo["${ref%:*}"]=1
done
for ref in "${bases[@]}"; do used_image["$(norm "$ref")"]=1; done
unused=()
while IFS= read -r image; do
  [[ "$image" == node-rental-* || "$image" == *'<none>'* ]] && continue
  [[ -n "${used_image[$image]:-}" ]] && continue
  if [[ -n "${used_repo[${image%:*}]:-}" ]]; then
    if ((AUTO)); then
      # Re-downloadable, but it may be the version to go back to after an
      # upgrade of that service: the owner decides.
      unused+=("$image")
    elif ((APPLY)); then
      # No -f: an image a container still uses is refused.
      if docker rmi "$image" >/dev/null 2>&1; then
        removed_n=$((removed_n + 1))
        say "  removed: $image"
      else
        say "  kept (in use): $image"
      fi
    else
      say "  would remove: $image (no container uses it; ${image%:*} runs another version)"
    fi
  else
    unused+=("$image")
  fi
done < <(docker images --format '{{.Repository}}:{{.Tag}}' | sort -u)

say ""
say "== Only listed: decide yourself"
if ((${#unused[@]})); then
  say "  images no container uses (remove: docker rmi <name>):"
  for image in "${unused[@]}"; do
    say "    $image ($(docker image inspect -f '{{.Size}}' "$image" 2>/dev/null | awk '{ printf "%d MB", $1 / 1048576 }'))"
  done
fi
mapfile -t stopped < <(docker ps -a --filter status=exited --filter status=created --format '{{.Names}} ({{.Image}}, {{.Status}})')
if ((${#stopped[@]})); then
  say "  stopped containers (remove: docker rm <name>):"
  printf '    %s\n' "${stopped[@]}"
fi
mapfile -t volumes < <(docker volume ls -q --filter dangling=true)
if ((${#volumes[@]})); then
  say "  volumes no container uses; their data is lost on removal (docker volume rm <name>):"
  printf '    %s\n' "${volumes[@]}"
fi
while IFS=$'\t' read -r size file; do
  say "  large file untouched for 2 weeks: $file ($((size / MB)) MB); check, then: rm <path>"
done < <(find "$HOME_DIR" -xdev -type f -size +50M -mtime +14 \
  \( -name '*.tar*' -o -name '*.zip' -o -name '*.gz' -o -name '*.img' -o -name '*.iso' -o -name '*.log' \) \
  -not -path "$HOME_DIR/server-backups/*" -printf '%s\t%p\n' 2>/dev/null | sort -rn | head -n 15)
manual_n=$((${#unused[@]} + ${#stopped[@]} + ${#volumes[@]}))

say ""
say "== Container logs over $LOG_REPORT_MB MB (emptied above $LOG_EMPTY_MB MB)"
while IFS= read -r name; do
  log="$(docker inspect -f '{{.LogPath}}' "$name" 2>/dev/null)"
  [[ -n "$log" && -f "$log" ]] || continue
  size=$(($(stat -c %s "$log") / MB))
  ((size > LOG_REPORT_MB)) || continue
  if ((size > LOG_EMPTY_MB)); then
    if ((APPLY)); then
      truncate -s 0 "$log" && say "  emptied: $name ($size MB)"
    else
      say "  would empty: $name ($size MB); limit it in its compose file: logging max-size"
    fi
  else
    say "  $name: $size MB"
  fi
done < <(docker ps -a --format '{{.Names}}')

say ""
if ((AUTO)); then
  say "== Old copies in the project folder (the newest $KEEP_COPIES and everything younger than $MIN_AGE_DAYS days are kept)"
else
  say "== Old copies in the project folder (the newest $KEEP_COPIES are kept)"
fi
prune_newest "$WORKDIR/data" 'before-*.db' "$KEEP_COPIES"
prune_newest "$WORKDIR/data" '*.migrate-check*' 0
prune_newest "$WORKDIR" '.env.before-*' "$KEEP_COPIES"
# Old keys: they are only needed until the new key is verified by --doctor.
if ((AUTO)); then
  shopt -s nullglob
  old_keys=("$WORKDIR"/secrets/*.bak-*)
  shopt -u nullglob
  ((${#old_keys[@]})) && say "  old keys in secrets/ are left for the supervised cleanup: ${#old_keys[@]} file(s)"
else
  prune_newest "$WORKDIR/secrets" '*.bak-*' 0
fi

shopt -s nullglob
plain=("$WORKDIR"/data/*.plain-backup)
shopt -u nullglob
if ((${#plain[@]})); then
  # A plaintext copy of the database is a risk: remove it only when the encrypted
  # database passes its check.
  if docker exec "$CONTAINER" python scripts/db_tool.py check /app/data/bot.db >/dev/null 2>&1; then
    remove "${plain[@]}"
  else
    say "  kept: ${plain[*]} (the encrypted database did not pass its check)"
  fi
fi

say ""
say "== Old delivery folders outside the project"
for path in "$HOME_DIR/vpn-update" "$HOME_DIR/vpn-new" "$HOME_DIR/vpn-new.zip" "$HOME_DIR/vpn-update.zip"; do
  [[ -e "$path" ]] || continue
  if ((AUTO)); then
    # An update may be about to start from this folder.
    say "  left for the supervised cleanup: $path"
  else
    remove "$path"
  fi
done

say ""
say "== System"
journalctl --disk-usage 2>/dev/null
if ((APPLY)); then
  journalctl --vacuum-time=14d >/dev/null 2>&1 && say "  journal kept for 14 days"
  apt-get clean >/dev/null 2>&1 && say "  apt cache cleared"
else
  say "  would run: journalctl --vacuum-time=14d, apt-get clean"
fi
# Old kernels only, as apt itself would pick them; never the running one.
mapfile -t kernels < <(apt-get -s autoremove 2>/dev/null | awk '/^Remv / { print $2 }' \
  | grep -E '^linux-(image|headers|modules|modules-extra|hwe)-' | grep -vF -- "$(uname -r)")
if ((${#kernels[@]})); then
  if ((AUTO)); then
    # A kernel change touches the boot files: only with someone at the keyboard.
    say "  old kernels are left for the supervised cleanup: ${kernels[*]}"
  elif ((APPLY)); then
    if DEBIAN_FRONTEND=noninteractive apt-get -y -q purge "${kernels[@]}" >/dev/null 2>&1; then
      say "  old kernels removed: ${kernels[*]}"
    else
      say "  old kernels could not be removed: ${kernels[*]}"
    fi
  else
    say "  would remove old kernels: ${kernels[*]}"
  fi
fi

if ((!AUTO)); then
  say ""
  say "== Bot database"
  docker exec "$CONTAINER" python scripts/db_maintenance.py report /app/data/bot.db ||
    say "  report not available: the running bot image predates db_maintenance.py; update the bot first"
  if ((APPLY)); then
    docker exec "$CONTAINER" python scripts/db_maintenance.py clean /app/data/bot.db --apply ||
      say "  database cleanup did not finish: see above; a copy may be in data/before-clean-*.db"
  else
    docker exec "$CONTAINER" python scripts/db_maintenance.py clean /app/data/bot.db || true
  fi
fi

say ""
if ((!AUTO)); then
  if ((APPLY)); then
    say "DONE. Check: bash scripts/update_bot.sh --doctor"
  else
    say "REPORT DONE. To remove the items above: bash scripts/server_cleanup.sh --apply"
  fi
  exit 0
fi

if ((DRY)); then
  say "CLEANUP_AUTO_DRY_RUN: nothing was removed. The weekly run does the items marked 'would remove'."
  exit 0
fi

# ----- the weekly run is over: did anything change that should not have?
running_after="$(running_containers)"
if [[ "$running_after" != "$running_before" ]]; then
  auto_fail "the running containers changed during the cleanup (before: $running_before; after: $running_after)" \
    "Автоочистка закончилась, но набор запущенных контейнеров изменился. Было: $running_before. Стало: $running_after. Проверьте: bash scripts/update_bot.sh --doctor"
fi
troubled="$(unhealthy_containers)"
if [[ -n "$troubled" ]]; then
  auto_fail "a container is not healthy after the cleanup: $troubled" \
    "После автоочистки контейнер не в порядке: $troubled. Проверьте: bash scripts/update_bot.sh --doctor"
fi

after_free="$(free_mb)"
after_free="${after_free:-0}"
freed=$((after_free - free_before))
((freed < 0)) && freed=0
free_gb="$(awk -v m="$after_free" 'BEGIN { printf "%.1f", m / 1024 }')"

# The owner hears about the items left to them only when there are more than last time.
previous="$(sed -n 's/^manual=//p' "$STATE_DIR/last-run" 2>/dev/null | head -n1)"
[[ "$previous" =~ ^[0-9]+$ ]] || previous=-1
state_write ok "removed $removed_n item(s), freed $freed MB"

say "CLEANUP_AUTO_OK removed=$removed_n freed=${freed}MB free=${free_gb}GB"
if ((after_free < LOW_FREE_MB)); then
  notify problem "⚠️ На сервере мало места: свободно $free_gb ГБ. Автоочистка убрала только то, что безопасно без присмотра. Запустите вручную: bash scripts/server_cleanup.sh (отчёт), затем с --apply."
else
  text="🧹 Еженедельная очистка сервера: освобождено $freed МБ, свободно $free_gb ГБ."
  if ((manual_n > previous && manual_n > 0)); then
    parts=()
    ((${#stopped[@]})) && parts+=("остановленных контейнеров: ${#stopped[@]}")
    ((${#volumes[@]})) && parts+=("томов без контейнера: ${#volumes[@]}")
    ((${#unused[@]})) && parts+=("образов без контейнера: ${#unused[@]}")
    list=""
    for part in "${parts[@]}"; do list+="${list:+, }$part"; done
    text+=" Ничего из этого не удалялось, решение за вами ($list): bash scripts/server_cleanup.sh"
  fi
  notify always "$text"
fi
