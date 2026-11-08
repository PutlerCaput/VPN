#!/usr/bin/env bash
# Server cleanup: old Docker images and build cache, old backups and copies of
# secrets, leftover delivery folders, oversized container logs, old kernels,
# system journal, and technical rows of the bot database.
#
#   bash scripts/server_cleanup.sh            # report only: removes nothing
#   bash scripts/server_cleanup.sh --apply    # removes what the report lists
#
# Never removed: running images, the newest two rollback images of each service,
# :latest, the base images of the bot and the site, payments and legal records,
# the GitHub checkout, the encrypted copies made by backup.sh, the running
# kernel. The database loses only technical rows (db/maintenance.py) and gets
# a checked copy first. Stopped containers, volumes, images no container uses
# and large files are only listed: whether they are needed is your call.
set -o pipefail

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1
CONTAINER="${CONTAINER:-node-rental-bot-1}"
KEEP_ROLLBACKS=2
KEEP_COPIES=2
MB=$((1024 * 1024))
LOG_REPORT_MB=50
LOG_EMPTY_MB=200
SRC="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"

say() { printf '%s\n' "$*"; }

remove() {  # remove <path>...
  local path
  for path in "$@"; do
    if ((APPLY)); then
      if rm -rf -- "$path"; then say "  removed: $path"; else say "  FAILED: $path"; fi
    else
      say "  would remove: $path"
    fi
  done
}

prune_newest() {  # prune_newest <directory> <name pattern> <how many to keep>
  local dir="$1" pattern="$2" keep="$3" n=0 line file
  [[ -d "$dir" ]] || return 0
  while IFS= read -r line; do
    file="${line#* }"
    n=$((n + 1))
    if ((n <= keep)); then say "  keep: $file"; else remove "$file"; fi
  done < <(find "$dir" -maxdepth 1 -type f -name "$pattern" -printf '%T@ %p\n' 2>/dev/null | sort -rn)
}

# Names the containers were created with: update_web.sh reads them as its
# current image, so an image named by any container must keep its tag.
mapfile -t in_use < <(docker ps -a --format '{{.Image}}' | sort -u)

WORKDIR="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$CONTAINER" 2>/dev/null)"
if [[ -z "$WORKDIR" || ! -d "$WORKDIR" ]]; then
  say "bot container $CONTAINER not found: nothing done"
  exit 1
fi

if ((APPLY)); then say "== APPLY: the items below are removed"; else say "== REPORT ONLY: nothing is removed"; fi

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
    if ((kept)); then
      say "  keep: $tag"
    elif ((APPLY)); then
      # No -f: an image used by a running or stopped container is refused.
      if docker rmi "$tag" >/dev/null 2>&1; then say "  removed: $tag"; else say "  kept (in use): $tag"; fi
    else
      say "  would remove: $tag"
    fi
  done
done
if ((APPLY)); then
  docker image prune -f >/dev/null 2>&1 && say "  dangling images removed"
  docker builder prune -f --filter until=168h >/dev/null 2>&1 && say "  build cache older than 7 days removed"
else
  say "  would remove: dangling images and build cache older than 7 days"
fi

say ""
say "== Other versions of running services' images (re-downloaded if ever needed)"
mapfile -t bases < <(awk 'toupper($1) == "FROM" { print $2 }' "$SRC/Dockerfile" 2>/dev/null | sort -u)
declare -A used_repo=()
for ref in "${in_use[@]}"; do
  [[ "${ref##*:}" == */* || "$ref" != *:* ]] && ref="$ref:latest"
  used_repo["${ref%:*}"]=1
done
unused=()
while IFS= read -r image; do
  [[ "$image" == node-rental-* || "$image" == *'<none>'* ]] && continue
  skip=0
  for name in "${in_use[@]}" "${bases[@]}"; do [[ "$image" == "$name" ]] && skip=1; done
  ((skip)) && continue
  if [[ -n "${used_repo[${image%:*}]:-}" ]]; then
    if ((APPLY)); then
      # No -f: an image a container still uses is refused.
      if docker rmi "$image" >/dev/null 2>&1; then say "  removed: $image"; else say "  kept (in use): $image"; fi
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
done < <(find /root -xdev -type f -size +50M -mtime +14 \
  \( -name '*.tar*' -o -name '*.zip' -o -name '*.gz' -o -name '*.img' -o -name '*.iso' -o -name '*.log' \) \
  -not -path '/root/server-backups/*' -printf '%s\t%p\n' 2>/dev/null | sort -rn | head -n 15)

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
say "== Old copies in the project folder (the newest $KEEP_COPIES are kept)"
prune_newest "$WORKDIR/data" 'before-*.db' "$KEEP_COPIES"
prune_newest "$WORKDIR/data" '*.migrate-check*' 0
prune_newest "$WORKDIR" '.env.before-*' "$KEEP_COPIES"
# Old keys: they are only needed until the new key is verified by --doctor.
prune_newest "$WORKDIR/secrets" '*.bak-*' 0

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
for path in /root/vpn-update /root/vpn-new /root/vpn-new.zip /root/vpn-update.zip; do
  [[ -e "$path" ]] && remove "$path"
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
  if ((APPLY)); then
    if DEBIAN_FRONTEND=noninteractive apt-get -y -q purge "${kernels[@]}" >/dev/null 2>&1; then
      say "  old kernels removed: ${kernels[*]}"
    else
      say "  old kernels could not be removed: ${kernels[*]}"
    fi
  else
    say "  would remove old kernels: ${kernels[*]}"
  fi
fi

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

say ""
if ((APPLY)); then
  say "DONE. Check: bash scripts/update_bot.sh --doctor"
else
  say "REPORT DONE. To remove the items above: bash scripts/server_cleanup.sh --apply"
fi
