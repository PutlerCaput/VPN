#!/usr/bin/env bash
# Downloads geosite/geoip and the malware/phishing lists into the Xray assets
# directory of a Marzban node, verifies SHA-256, then restarts Marzban.
# Cron (weekly): 0 5 * * 1 /opt/node-rental/deploy/xray/update_geodata.sh
set -euo pipefail

ASSETS="${XRAY_ASSETS_PATH:-/var/lib/marzban/assets}"
SOURCE="${GEODATA_SOURCE:-https://raw.githubusercontent.com/Chocolate4U/Iran-v2ray-rules/release}"
FILES=(geosite.dat geoip.dat security.dat security-ip.dat)

mkdir -p "$ASSETS"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

for file in "${FILES[@]}"; do
  curl -fsSL --retry 3 -o "$TMP/$file" "$SOURCE/$file"
  expected="$(curl -fsSL --retry 3 "$SOURCE/$file.sha256sum" | cut -d' ' -f1)"
  actual="$(sha256sum "$TMP/$file" | cut -d' ' -f1)"
  if [[ -z "$expected" || "$expected" != "$actual" ]]; then
    echo "checksum mismatch for $file, nothing replaced" >&2
    exit 1
  fi
done

for file in "${FILES[@]}"; do
  install -m 644 "$TMP/$file" "$ASSETS/$file"
done

if command -v marzban >/dev/null 2>&1; then
  marzban restart -n
fi
echo "✅ Geodata updated in $ASSETS"
