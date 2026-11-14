#!/usr/bin/env bash
# Rebuilds the AmneziaWG DNS filter's block zone from public lists and reloads the
# resolver. Run on the server from the repository checkout:
#
#   bash scripts/awg_dns_update.sh            download, check, build, reload
#   bash scripts/awg_dns_update.sh --status   how many domains are blocked now
#
# The lists come from fixed URLs below. A download that fails, a list that is too
# small or too large, or a build that is refused keeps the previous block zone:
# the filter never switches off by accident and never blocks everything.
# Nothing here changes the tunnel, the keys or the users.
set -euo pipefail

CONTAINER="${AWG_CONTAINER:-cyberdefency-awg}"
ZONE="/var/lib/awg/dns/blocklist.conf"
LISTS=(
  "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/tif-onlydomains.txt"
  "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/nsfw-onlydomains.txt"
  "https://raw.githubusercontent.com/hagezi/dns-blocklists/main/wildcard/gambling-onlydomains.txt"
  "https://phishing.army/download/phishing_army_blocklist.txt"
)

say() { printf '%s\n' "$*"; }
stop() {
  printf 'AWG_DNS_UPDATE_STOPPED: %s\n' "$*" >&2
  exit 1
}

if [[ "${1:-}" == "--status" ]]; then
  docker exec "$CONTAINER" sh -c "grep -c '^local-zone' '$ZONE' || true" \
    | sed 's/^/blocked domains: /'
  exit 0
fi

docker inspect "$CONTAINER" >/dev/null 2>&1 || stop "no container $CONTAINER"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
files=()
index=0
for url in "${LISTS[@]}"; do
  index=$((index + 1))
  target="$work/list-$index.txt"
  if ! curl -fsSL --max-time 300 --max-filesize 120000000 -o "$target" "$url"; then
    stop "download failed: $url (the previous block zone stays)"
  fi
  files+=("$target")
done
say "downloaded ${#files[@]} lists"

remote=()
for file in "${files[@]}"; do
  name="/tmp/$(basename "$file")"
  docker cp "$file" "$CONTAINER:$name"
  remote+=("$name")
done

if ! docker exec "$CONTAINER" python3 /opt/awg/dns/blocklist.py --out "$ZONE" "${remote[@]}"; then
  docker exec "$CONTAINER" rm -f "${remote[@]}" || true
  stop "the build was refused; the previous block zone stays"
fi
docker exec "$CONTAINER" rm -f "${remote[@]}" || true

# PID 1 of the container is the agent: SIGHUP makes it reload the resolver.
docker kill -s HUP "$CONTAINER" >/dev/null
say "AWG_DNS_UPDATE_OK: block zone rebuilt and the resolver reloaded"
say "Check: bash scripts/awg_dns_update.sh --status"
