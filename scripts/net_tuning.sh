#!/usr/bin/env bash
# BBR, fq and larger TCP buffers for a VPN server (DE or RU).
# The eight settings are the ones in docs/ANTI_CENSORSHIP_AUDIT.md, section 8.
#
#   bash scripts/net_tuning.sh            apply; safe to run again
#   bash scripts/net_tuning.sh --apply    same
#   bash scripts/net_tuning.sh --check    print current values, change nothing
#
# Writes /etc/sysctl.d/90-vpn-tuning.conf, loads tcp_bbr and lists it in
# /etc/modules-load.d/bbr.conf, runs sysctl --system, and sets the root qdisc
# of the default-route interface to fq (tc qdisc replace).
set -euo pipefail

SYSCTL="${SYSCTL_BIN:-sysctl}"
TC="${TC_BIN:-tc}"
MODPROBE="${MODPROBE_BIN:-modprobe}"
IP="${IP_BIN:-ip}"
SYSCTL_CONF="${SYSCTL_CONF:-/etc/sysctl.d/90-vpn-tuning.conf}"
MODULES_CONF="${MODULES_CONF:-/etc/modules-load.d/bbr.conf}"

say() { printf '%s\n' "$*"; }
stop() {
  printf 'NET_TUNING_STOPPED: %s\n' "$*" >&2
  exit 1
}

# Exactly the eight lines from the audit. Nothing else belongs in the file.
tuning_text() {
  cat <<'EOF'
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_rmem = 4096 87380 16777216
net.ipv4.tcp_wmem = 4096 65536 16777216
net.ipv4.tcp_mtu_probing = 1
net.ipv4.tcp_fastopen = 3
EOF
}

default_iface() {
  "$IP" route show default 2>/dev/null | awk '
    $1 == "default" {
      for (i = 1; i <= NF; i++) if ($i == "dev" && (i + 1) <= NF) {
        print $(i + 1)
        exit
      }
    }
  ' || true
}

# Write stdin to path, leaving the file untouched when the bytes already match.
install_text() {
  local path="$1" dir tmp
  dir="$(dirname "$path")"
  mkdir -p "$dir"
  tmp="$(mktemp "$dir/.net-tuning.XXXXXX")"
  cat >"$tmp"
  if [[ -f "$path" ]] && cmp -s "$tmp" "$path"; then
    rm -f "$tmp"
    return 0
  fi
  chmod 644 "$tmp"
  mv "$tmp" "$path"
}

print_state() {
  local line key iface qdisc
  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    key="${line%% =*}"
    say "$key = $("$SYSCTL" -n "$key" 2>/dev/null || echo unknown)"
  done < <(tuning_text)
  iface="$(default_iface)"
  if [[ -n "$iface" ]]; then
    qdisc="$("$TC" qdisc show dev "$iface" 2>/dev/null || echo unknown)"
    say "qdisc($iface): $qdisc"
  else
    say "qdisc: unknown"
  fi
}

check() {
  print_state
  say "NET_TUNING_CHECK_OK: nothing changed"
}

apply() {
  local iface cc qdisc
  tuning_text | install_text "$SYSCTL_CONF"
  printf '%s\n' tcp_bbr | install_text "$MODULES_CONF"
  "$MODPROBE" tcp_bbr || stop "cannot load tcp_bbr"
  "$SYSCTL" --system >/dev/null || stop "sysctl --system failed"
  iface="$(default_iface)"
  [[ -n "$iface" ]] || stop "no default route; cannot set the fq qdisc"
  "$TC" qdisc replace dev "$iface" root fq || stop "cannot set fq on $iface"
  cc="$("$SYSCTL" -n net.ipv4.tcp_congestion_control 2>/dev/null || echo unknown)"
  qdisc="$("$TC" qdisc show dev "$iface" 2>/dev/null || echo unknown)"
  say "congestion_control: $cc"
  say "qdisc($iface): $qdisc"
  say "NET_TUNING_OK: $cc on $iface"
}

case "${1:-}" in
  "" | --apply) apply ;;
  --check) check ;;
  *) stop "usage: net_tuning.sh [--check|--apply]" ;;
esac
