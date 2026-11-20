#!/usr/bin/env bash
# Read-only diagnostics on one VPS. No system changes, secrets or SSH setup.
set -euo pipefail

printf '=== Host (not yet trusted until provider SSH key checked) ===\n'
hostname
date -u '+UTC %Y-%m-%dT%H:%M:%SZ'
uname -srmo
if [[ -r /etc/os-release ]]; then
  grep -E '^(ID|VERSION_ID)=' /etc/os-release || true
fi

printf '\n=== Resources ===\n'
free -h || true
df -h / || true

printf '\n=== IPv4 route and interfaces ===\n'
ip -4 -brief addr || true
ip -4 route show default || true

printf '\n=== Listening TCP ports (no credentials) ===\n'
ss -lnt || true

printf '\n=== Service availability ===\n'
for binary in docker xray haproxy; do
  if command -v "$binary" >/dev/null 2>&1; then
    printf '%s: present\n' "$binary"
  else
    printf '%s: missing\n' "$binary"
  fi
done

printf '\n=== Failed units ===\n'
systemctl --failed --no-pager --plain || true

printf '\nREAD_ONLY_PREFLIGHT_COMPLETE\n'
