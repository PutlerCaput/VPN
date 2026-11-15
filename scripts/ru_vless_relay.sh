#!/usr/bin/env bash
# VLESS through the RU server: RU forwards TCP to DE, so clients connect to the
# Russian address. Runs ON THE RU SERVER. Without the repository there, pipe it
# from DE:
#
#   ssh de-ru 'cat /root/vpn-src/scripts/ru_vless_relay.sh' \
#     | ssh root@45.138.157.65 'bash -s -- --check'
#
#   --check    what is there now; changes nothing
#   --apply    put the forwarding in place and save the rules
#   --remove   take the forwarding away and save
#
# The rules are the ones live on RU (docs/VPN_ENTRIES.md):
#   TCP 443, 2087, 8443 -> DE:2096
#   TCP 2083            -> DE:2098
#   MASQUERADE for those two DE ports
#   MSS clamp on forwarded SYN packets, so large packets do not stall on
#   mobile networks
# FORWARD policy on RU is ACCEPT, so no filter FORWARD rules are added.
# The INPUT firewall of ru_firewall.sh is not touched. Nothing secret is printed.
# After a reinstall, --apply is the one command that restores this. On RU also
# run scripts/net_tuning.sh (BBR and fq).
set -euo pipefail

DE_IP="${DE_IP:-89.125.210.172}"
IPT="${IPTABLES_BIN:-iptables}"
SYSCTL="${SYSCTL_BIN:-sysctl}"
SAVE="${SAVE_BIN:-netfilter-persistent}"

say() { printf '%s\n' "$*"; }
stop() {
  printf 'RU_VLESS_RELAY_STOPPED: %s\n' "$*" >&2
  exit 1
}

MODE="${1:-}"
[[ $# -le 1 ]] || stop "usage: --check | --apply | --remove"
[[ "$DE_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || stop "DE_IP is not an IPv4 address"

# table, chain, how to add (A = append, I = insert first), rule
RULES=(
  "nat|PREROUTING|A|-p tcp --dport 443 -j DNAT --to-destination $DE_IP:2096"
  "nat|PREROUTING|A|-p tcp --dport 2087 -j DNAT --to-destination $DE_IP:2096"
  "nat|PREROUTING|A|-p tcp --dport 8443 -j DNAT --to-destination $DE_IP:2096"
  "nat|PREROUTING|A|-p tcp --dport 2083 -j DNAT --to-destination $DE_IP:2098"
  "nat|POSTROUTING|A|-d $DE_IP -p tcp --dport 2096 -j MASQUERADE"
  "nat|POSTROUTING|A|-d $DE_IP -p tcp --dport 2098 -j MASQUERADE"
  "mangle|FORWARD|A|-p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu"
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

case "$MODE" in
  --check)
    say "ip_forward: $("$SYSCTL" -n net.ipv4.ip_forward 2>/dev/null || echo unknown)"
    show
    say "RU_VLESS_RELAY_CHECK_OK: nothing changed. Switch on with: --apply"
    ;;
  --apply)
    [[ "$("$SYSCTL" -n net.ipv4.ip_forward 2>/dev/null)" == 1 ]] \
      || stop "net.ipv4.ip_forward is not 1 on this server"
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
    say "RU_VLESS_RELAY_OK: TCP 443/2087/8443 -> $DE_IP:2096, TCP 2083 -> $DE_IP:2098 ($added rules added, saved)"
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
    say "RU_VLESS_RELAY_REMOVED: $removed rules removed, saved"
    ;;
  *)
    stop "usage: --check | --apply | --remove"
    ;;
esac
