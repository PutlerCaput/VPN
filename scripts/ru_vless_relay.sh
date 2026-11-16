#!/usr/bin/env bash
# VLESS through the RU server: RU forwards TCP to DE, so clients connect to the
# Russian address. Runs ON THE RU SERVER. Without the repository there, pipe it
# from DE:
#
#   ssh de-ru 'cat /root/vpn-src/scripts/ru_vless_relay.sh' \
#     | ssh root@45.138.157.65 'bash -s -- --check'
#
#   --check    what is there now; changes nothing
#   --apply    put the forwarding in place, drop the old Marzban forwards,
#              save the rules
#   --remove   take the forwarding (and any old Marzban forward) away and save
#
# The rules are the ones live on RU since the move to Hiddify Manager
# (16 November 2026, docs/VPN_ENTRIES.md):
#   TCP 443, 2087, 8443, 2083, 2085 -> DE:2443 (Hiddify: VLESS Reality, XHTTP)
#   MASQUERADE for DE:2443
#   MSS clamp on forwarded SYN packets, so large packets do not stall on
#   mobile networks
# The Marzban forwards (-> DE:2096 and DE:2098) are obsolete: --apply removes
# them after the new rules are in, so there is no moment without a forward.
# AmneziaWG (UDP 39051 and 44877) is a separate set of rules and is never
# touched here. FORWARD policy on RU is ACCEPT, so no filter FORWARD rules are
# added. The INPUT firewall of ru_firewall.sh is not touched. Before any change
# the current rules are copied with iptables-save. Nothing secret is printed.
# After a reinstall, --apply is the one command that restores this. On RU also
# run scripts/net_tuning.sh (BBR and fq).
set -euo pipefail

DE_IP="${DE_IP:-89.125.210.172}"
DE_PORT="${DE_PORT:-2443}"
IPT="${IPTABLES_BIN:-iptables}"
IPT_SAVE="${IPTABLES_SAVE_BIN:-iptables-save}"
SYSCTL="${SYSCTL_BIN:-sysctl}"
SAVE="${SAVE_BIN:-netfilter-persistent}"
BACKUP_DIR="${BACKUP_DIR:-/etc/iptables}"

say() { printf '%s\n' "$*"; }
stop() {
  printf 'RU_VLESS_RELAY_STOPPED: %s\n' "$*" >&2
  exit 1
}

MODE="${1:-}"
[[ $# -le 1 ]] || stop "usage: --check | --apply | --remove"
[[ "$DE_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || stop "DE_IP is not an IPv4 address"
if ! [[ "$DE_PORT" =~ ^[0-9]+$ ]] || ((10#$DE_PORT < 1 || 10#$DE_PORT > 65535)); then
  stop "DE_PORT is not a TCP port"
fi

# table, chain, how to add (A = append, I = insert first), rule
RULES=(
  "nat|PREROUTING|A|-p tcp --dport 443 -j DNAT --to-destination $DE_IP:$DE_PORT"
  "nat|PREROUTING|A|-p tcp --dport 2087 -j DNAT --to-destination $DE_IP:$DE_PORT"
  "nat|PREROUTING|A|-p tcp --dport 8443 -j DNAT --to-destination $DE_IP:$DE_PORT"
  "nat|PREROUTING|A|-p tcp --dport 2083 -j DNAT --to-destination $DE_IP:$DE_PORT"
  "nat|PREROUTING|A|-p tcp --dport 2085 -j DNAT --to-destination $DE_IP:$DE_PORT"
  "nat|POSTROUTING|A|-d $DE_IP -p tcp --dport $DE_PORT -j MASQUERADE"
  "mangle|FORWARD|A|-p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu"
)

# Marzban (removed on 16 November 2026) listened on DE:2096 and DE:2098. While
# these DNAT rules stay, they win over the new ones for the same ports.
OBSOLETE=(
  "nat|PREROUTING|-p tcp --dport 443 -j DNAT --to-destination $DE_IP:2096"
  "nat|PREROUTING|-p tcp --dport 2087 -j DNAT --to-destination $DE_IP:2096"
  "nat|PREROUTING|-p tcp --dport 8443 -j DNAT --to-destination $DE_IP:2096"
  "nat|PREROUTING|-p tcp --dport 2083 -j DNAT --to-destination $DE_IP:2098"
  "nat|POSTROUTING|-d $DE_IP -p tcp --dport 2096 -j MASQUERADE"
  "nat|POSTROUTING|-d $DE_IP -o eth0 -p tcp --dport 2096 -j MASQUERADE"
  "nat|POSTROUTING|-d $DE_IP -p tcp --dport 2098 -j MASQUERADE"
)

has_rule() {
  local table="$1" chain="$2" spec="$3"
  # shellcheck disable=SC2086  # the spec is a fixed list of words built above
  "$IPT" -t "$table" -C "$chain" $spec >/dev/null 2>&1
}

missing_count() {
  local entry table chain how spec n=0
  for entry in "${RULES[@]}"; do
    IFS='|' read -r table chain how spec <<<"$entry"
    has_rule "$table" "$chain" "$spec" || n=$((n + 1))
  done
  printf '%s\n' "$n"
}

obsolete_count() {
  local entry table chain spec n=0
  for entry in "${OBSOLETE[@]}"; do
    IFS='|' read -r table chain spec <<<"$entry"
    has_rule "$table" "$chain" "$spec" && n=$((n + 1))
  done
  printf '%s\n' "$n"
}

present_count() {
  local entry table chain how spec n=0
  for entry in "${RULES[@]}"; do
    IFS='|' read -r table chain how spec <<<"$entry"
    has_rule "$table" "$chain" "$spec" && n=$((n + 1))
  done
  printf '%s\n' "$n"
}

# A copy of every table before the first change; restore with iptables-restore.
backup_rules() {
  local file
  file="$BACKUP_DIR/rules.v4.before-ru-vless-relay-$(date -u +%Y%m%dT%H%M%SZ)"
  mkdir -p "$BACKUP_DIR" || stop "cannot create $BACKUP_DIR"
  (umask 077 && "$IPT_SAVE" >"$file") || stop "cannot back up the current rules to $file"
  say "backup: $file"
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
  for entry in "${OBSOLETE[@]}"; do
    IFS='|' read -r table chain spec <<<"$entry"
    if has_rule "$table" "$chain" "$spec"; then
      say "obsolete: $table $chain $spec"
    fi
  done
}

drop_obsolete() {
  local entry table chain spec n=0
  for entry in "${OBSOLETE[@]}"; do
    IFS='|' read -r table chain spec <<<"$entry"
    while has_rule "$table" "$chain" "$spec"; do
      # shellcheck disable=SC2086
      "$IPT" -t "$table" -D "$chain" $spec
      n=$((n + 1))
    done
  done
  printf '%s\n' "$n"
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
    dropped=0
    if (($(missing_count) > 0 || $(obsolete_count) > 0)); then
      backup_rules
      # New rules first, then the old ones go: no moment without a forward.
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
      dropped="$(drop_obsolete)"
    fi
    "$SAVE" save >/dev/null 2>&1 || stop "rules are active but not saved: run $SAVE save"
    show
    say "RU_VLESS_RELAY_OK: TCP 443/2087/8443/2083/2085 -> $DE_IP:$DE_PORT ($added rules added, $dropped obsolete removed, saved)"
    ;;
  --remove)
    removed=0
    if (($(present_count) > 0 || $(obsolete_count) > 0)); then
      backup_rules
      for entry in "${RULES[@]}"; do
        IFS='|' read -r table chain how spec <<<"$entry"
        while has_rule "$table" "$chain" "$spec"; do
          # shellcheck disable=SC2086
          "$IPT" -t "$table" -D "$chain" $spec
          removed=$((removed + 1))
        done
      done
      removed=$((removed + $(drop_obsolete)))
    fi
    "$SAVE" save >/dev/null 2>&1 || stop "rules are removed but not saved: run $SAVE save"
    say "RU_VLESS_RELAY_REMOVED: $removed rules removed, saved"
    ;;
  *)
    stop "usage: --check | --apply | --remove"
    ;;
esac
