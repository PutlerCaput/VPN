#!/usr/bin/env bash
# Read-only diagnostics on one VPS. No system changes, secrets or SSH setup.
set -euo pipefail

inspect() {
  if ! "$@"; then
    printf 'Inspection failed or unavailable: %s\n' "$1"
  fi
}

printf '=== Host (private output; verify the SSH key before running) ===\n'
date -u '+UTC %Y-%m-%dT%H:%M:%SZ'
uname -srmo
if [[ -r /etc/os-release ]]; then
  grep -E '^(ID|VERSION_ID)=' /etc/os-release || true
fi

printf '\n=== Resources ===\n'
inspect free -h
inspect df -h /

printf '\n=== Clock synchronization ===\n'
inspect timedatectl show --property=NTPSynchronized --property=NTP

printf '\n=== Interfaces, routing and resolver ===\n'
inspect ip -brief address
inspect ip -4 route show default
inspect ip -6 route show default
if command -v resolvectl >/dev/null 2>&1; then
  inspect resolvectl dns
else
  inspect awk '$1 == "nameserver" { print $1, $2 }' /etc/resolv.conf
fi

printf '\n=== Listening TCP and UDP sockets ===\n'
inspect ss -lntu

printf '\n=== TCP/443 owner ===\n'
inspect ss -lntp 'sport = :443'

printf '\n=== Service versions ===\n'
inspect ssh -V
inspect docker --version
inspect xray version
inspect haproxy -v

printf '\n=== Relevant services ===\n'
inspect systemctl show ssh.service sshd.service docker.service xray.service haproxy.service \
  --property=Id --property=LoadState --property=ActiveState
if command -v docker >/dev/null 2>&1; then
  inspect docker ps --format '{{.Names}} {{.Status}} {{.Ports}}'
fi

printf '\n=== Host firewall (provider firewall must be checked separately) ===\n'
if (( EUID != 0 )); then
  printf 'Unprivileged run: firewall and TCP/443 process visibility may be incomplete.\n'
fi
firewall_found=false
if command -v nft >/dev/null 2>&1; then
  firewall_found=true
  inspect nft list ruleset
fi
for binary in iptables-save ip6tables-save; do
  if command -v "$binary" >/dev/null 2>&1; then
    firewall_found=true
    inspect "$binary"
  fi
done
if [[ "$firewall_found" == false ]]; then
  printf 'No supported firewall inspection command available.\n'
fi

printf '\n=== Failed units ===\n'
inspect systemctl --failed --no-pager --plain

printf '\nREAD_ONLY_PREFLIGHT_COMPLETE\n'
