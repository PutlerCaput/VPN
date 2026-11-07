#!/usr/bin/env bash
# Server cleanup: old Docker images and build cache, old backups and copies of
# secrets, leftover delivery folders, system journal, and technical rows of the
# bot database.
#
#   bash scripts/server_cleanup.sh            # report only: removes nothing
#   bash scripts/server_cleanup.sh --apply    # removes what the report lists
#
# Never removed: running images, the newest two rollback images of each service,
# :latest, payments and legal records, the GitHub checkout, the encrypted
# copies made by backup.sh. The database loses only technical rows
# (db/maintenance.py) and gets a checked copy first.
set -o pipefail

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1
CONTAINER="${CONTAINER:-node-rental-bot-1}"
KEEP_ROLLBACKS=2
KEEP_COPIES=2

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
