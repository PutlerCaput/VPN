#!/usr/bin/env bash
# AmneziaWG through the RU server: RU forwards a UDP port to the same port on DE,
# so clients connect to the Russian address when the German one is cut off.
# Runs ON THE RU SERVER. Without the repository there, pipe it from DE:
#
#   ssh de-ru 'cat /root/vpn-src/scripts/ru_awg_relay.sh' \
#     | ssh root@45.138.157.65 'bash -s -- --check'
#
#   --check  [PORT]   what is there now; changes nothing
#   --apply  [PORT]   forward UDP PORT to DE:PORT and save the rules
#   --remove [PORT]   take the forwarding away and save
#
# PORT is 39051 (the subscribers' AmneziaWG) unless given. The rules are the same
# kind as the VLESS entry's (DNAT + MASQUERADE, docs/VPN_ENTRIES.md): only nat
# PREROUTING/POSTROUTING and two FORWARD accepts for this one port. The INPUT
# firewall of ru_firewall.sh is not touched. Nothing secret is printed.
# After --apply, clients use it only with a config whose Endpoint is the RU
# address: on DE, scripts/awg_endpoint.sh --set 45.138.157.65.
set -euo pipefail

DE_IP="${DE_IP:-89.125.210.172}"
IPT="${IPTABLES_BIN:-iptables}"
SYSCTL="${SYSCTL_BIN:-sysctl}"
SS="${SS_BIN:-ss}"
SAVE="${SAVE_BIN:-netfilter-persistent}"

say() { printf '%s\n' "$*"; }
stop() {
  printf 'RU_AWG_RELAY_STOPPED: %s\n' "$*" >&2
  exit 1
}

MODE="${1:-}"
PORT="${2:-39051}"
[[ "$PORT" =~ ^[0-9]{4,5}$ ]] && ((PORT >= 1024 && PORT <= 65535)) \
  || stop "the port is a number, 1024-65535"
[[ "$DE_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || stop "DE_IP is not an IPv4 address"

# table, chain, how to add (A = append, I = insert first), rule
RULES=(
  "nat|PREROUTING|A|-p udp --dport $PORT -j DNAT --to-destination $DE_IP:$PORT"
  "nat|POSTROUTING|A|-d $DE_IP -p udp --dport $PORT -j MASQUERADE"
  "filter|FORWARD|I|-d $DE_IP -p udp --dport $PORT -j ACCEPT"
  "filter|FORWARD|I|-s $DE_IP -p udp --sport $PORT -j ACCEPT"
)

has_rule() {
  local table="$1" chain="$2" spec="$3"
  # shellcheck disable=SC2086  # the spec is a fixed list of words built above
  "$IPT" -t "$table" -C "$chain" $spec >/dev/null 2>&1
}

show() {
  local entry table chain how spec
  for entry in "${RULES[@]}"; do
    IFS='|' read -r table chain how spec <<<"$entry"
    if has_rule "$table" "$chain" "$spec"; then
      say "present: $table $chain $spec"
    else
      say "missing: $table $chain $spec"
    fi
  done
}

port_taken_here() {
  "$SS" -Hlun 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]$PORT\$"
}

case "$MODE" in
  --check)
    say "ip_forward: $("$SYSCTL" -n net.ipv4.ip_forward 2>/dev/null || echo unknown)"
    if port_taken_here; then
      say "WARNING: something on this server already listens on UDP $PORT"
    fi
    show
    say "RU_AWG_RELAY_CHECK_OK: nothing changed. Switch on with: --apply $PORT"
    ;;
  --apply)
    [[ "$("$SYSCTL" -n net.ipv4.ip_forward 2>/dev/null)" == 1 ]] \
      || stop "net.ipv4.ip_forward is not 1 on this server"
    if port_taken_here; then
      stop "UDP $PORT is already used by a program on this server; choose another port"
    fi
    added=0
    for entry in "${RULES[@]}"; do
      IFS='|' read -r table chain how spec <<<"$entry"
      if has_rule "$table" "$chain" "$spec"; then
        continue
      fi
      # shellcheck disable=SC2086
      if [[ "$how" == I ]]; then
        "$IPT" -t "$table" -I "$chain" 1 $spec
      else
        "$IPT" -t "$table" -A "$chain" $spec
      fi
      added=$((added + 1))
    done
    "$SAVE" save >/dev/null 2>&1 || stop "rules are active but not saved: run $SAVE save"
    show
    say "RU_AWG_RELAY_OK: UDP $PORT -> $DE_IP:$PORT ($added rules added, saved)"
    say "Next, on DE: bash /root/vpn-src/scripts/awg_endpoint.sh --set 45.138.157.65"
    ;;
  --remove)
    removed=0
    for entry in "${RULES[@]}"; do
      IFS='|' read -r table chain how spec <<<"$entry"
      while has_rule "$table" "$chain" "$spec"; do
        # shellcheck disable=SC2086
        "$IPT" -t "$table" -D "$chain" $spec
        removed=$((removed + 1))
      done
    done
    "$SAVE" save >/dev/null 2>&1 || stop "rules are removed but not saved: run $SAVE save"
    say "RU_AWG_RELAY_REMOVED: $removed rules removed, saved"
    ;;
  *)
    stop "usage: --check [PORT] | --apply [PORT] | --remove [PORT]"
    ;;
esac
