#!/usr/bin/env bash
# One sha256 of everything the Docker images are built from: the Dockerfile,
# .dockerignore and every source that a COPY line of the Dockerfile names.
#
#   bash scripts/source_digest.sh [project folder]    # default: this checkout
#
# update_bot.sh and update_web.sh stamp it on the images they build (the label
# node.rental.source). The monthly auto-update compares it with the folder as it
# is now: equal means a rebuild would ship the code that already runs, on fresher
# base images; different means the folder holds code nobody has deployed, and the
# auto-update leaves it alone.
set -euo pipefail

SRC="$(cd "${1:-$(dirname "$(readlink -f "$0")")/..}" && pwd)"
cd "$SRC"
[[ -f Dockerfile ]] || { echo "source_digest: no Dockerfile in $SRC" >&2; exit 1; }
shopt -s nullglob

paths=(Dockerfile)
[[ -f .dockerignore ]] && paths+=(.dockerignore)

# COPY lines, backslash continuations joined. "COPY --from=..." takes files from
# another build stage, not from the folder.
while IFS= read -r line; do
  read -ra words <<<"$line"
  sources=()
  from_stage=0
  for word in "${words[@]:1}"; do
    case "$word" in
      --from=*) from_stage=1 ;;
      --*) ;;
      *) sources+=("$word") ;;
    esac
  done
  ((from_stage == 0 && ${#sources[@]} >= 2)) || continue
  for word in "${sources[@]:0:$((${#sources[@]} - 1))}"; do
    # Unquoted on purpose: a source such as "app/*.py" is a pattern.
    # shellcheck disable=SC2206
    paths+=($word)
  done
done < <(sed -e ':a' -e '/\\$/N' -e 's/\\\n//' -e 'ta' Dockerfile | grep -E '^[[:space:]]*COPY[[:space:]]' || true)

{
  for path in "${paths[@]}"; do
    [[ -e "$path" ]] || continue
    find "$path" -type f ! -path '*/node_modules/*' ! -path '*/__pycache__/*' \
      ! -path '*/.git/*' ! -name '*.pyc' -print0
  done
} | LC_ALL=C sort -zu | xargs -r0 sha256sum | sha256sum | cut -d' ' -f1
