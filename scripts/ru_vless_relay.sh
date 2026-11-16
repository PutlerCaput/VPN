#!/usr/bin/env bash
# VLESS through the RU server: RU forwards TCP to DE, so clients connect to the
# Russian address. Runs ON THE RU SERVER. Without the repository there, pipe it
# from DE:
#
#   ssh de-ru 'cat /root/vpn-src/scripts/ru_vless_relay.sh' \
#     | ssh root@45.138.157.65 'bash -s -- --check'
#
#   --check    what is there now; changes nothing
#   --apply    put the forwarding in place, drop the obsolete forwards (Marzban,
#              direct DE:2443), save the rules
#   --remove   take the forwarding (and any obsolete forward) away and save
#
# The rules are the ones live on RU since the move to Hiddify Manager
# (16 November 2026, docs/VPN_ENTRIES.md):
#   TCP 443, 2087, 8443, 2083, 2085 -> DE 10.205.0.2:2443 through the wg-vpn
#   WireGuard tunnel (Hiddify: VLESS Reality, XHTTP). On the open internet the
#   RU -> DE hop drops many Reality connections; inside the tunnel it does not.
#   MASQUERADE for 10.205.0.2:2443 out of wg-vpn
#   UDP 36099 -> DE:36099 (Hiddify: Hysteria2 with Salamander obfuscation),
#   for mobile networks that cut TCP/TLS but pass UDP; MASQUERADE for it
#   UDP 443 -> DE:36099 as well: a spare Hysteria2 port that looks like QUIC
#   (HTTP/3) to a filter; HY2_BACKUP_PORT= (empty) leaves it out
#   nf_conntrack_max 65536 (/etc/sysctl.d/91-vpn-relay.conf): the kernel picks
#   7168 for 1 GB of RAM, too few NAT entries for many clients
#   MSS clamp on forwarded SYN packets, so large packets do not stall on
#   mobile networks
# The Marzban forwards (-> DE:2096 and DE:2098) and the direct TCP forwards to
# DE:2443 over the open internet are obsolete: --apply removes them after the
# new rules are in, so there is no moment without a forward. --apply needs the
# wg-vpn interface up (wg-quick@wg-vpn, docs/VPN_ENTRIES.md); without it the
# TCP entry would go nowhere, so the script stops and changes nothing.
# AmneziaWG (UDP 39051 and 44877) is a separate set of rules and is never
# touched here. FORWARD policy on RU is ACCEPT, so no filter FORWARD rules are
# added. The INPUT firewall of ru_firewall.sh is not touched. Before any change
# the current rules are copied with iptables-save. Nothing secret is printed.
# After a reinstall, --apply is the one command that restores this. On RU also
# run scripts/net_tuning.sh (BBR and fq).
set -euo pipefail
shopt -s inherit_errexit

DE_IP="${DE_IP:-89.125.210.172}"
DE_PORT="${DE_PORT:-2443}"
HY2_PORT="${HY2_PORT:-36099}"
HY2_BACKUP_PORT="${HY2_BACKUP_PORT-443}"
CONNTRACK_MAX="${CONNTRACK_MAX:-65536}"
RELAY_SYSCTL_CONF="${RELAY_SYSCTL_CONF:-/etc/sysctl.d/91-vpn-relay.conf}"
DE_TUNNEL_IP="${DE_TUNNEL_IP:-10.205.0.2}"
TUNNEL_IF="${TUNNEL_IF:-wg-vpn}"
IP_BIN="${IP_BIN:-ip}"
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
[[ "$DE_TUNNEL_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || stop "DE_TUNNEL_IP is not an IPv4 address"
# The direct forwards to DE_IP are removed as obsolete; the same address for the
# tunnel would remove the new rules right after adding them.
[[ "$DE_TUNNEL_IP" != "$DE_IP" ]] || stop "DE_TUNNEL_IP must be DE's address inside $TUNNEL_IF, not DE_IP"
[[ "$TUNNEL_IF" =~ ^[a-zA-Z0-9_-]{1,15}$ ]] || stop "TUNNEL_IF is not an interface name"
if ! [[ "$DE_PORT" =~ ^[0-9]+$ ]] || ((10#$DE_PORT < 1 || 10#$DE_PORT > 65535)); then
  stop "DE_PORT is not a TCP port"
fi
if ! [[ "$HY2_PORT" =~ ^[0-9]+$ ]] || ((10#$HY2_PORT < 1 || 10#$HY2_PORT > 65535)); then
  stop "HY2_PORT is not a UDP port"
fi
# AmneziaWG owns UDP 39051 and 44877 on RU; a second DNAT there would steal it.
[[ "$((10#$HY2_PORT))" != 39051 && "$((10#$HY2_PORT))" != 44877 ]] \
  || stop "HY2_PORT $HY2_PORT belongs to AmneziaWG"
HY2_PORT="$((10#$HY2_PORT))"
if [[ -n "$HY2_BACKUP_PORT" ]]; then
  if ! [[ "$HY2_BACKUP_PORT" =~ ^[0-9]+$ ]] || ((10#$HY2_BACKUP_PORT < 1 || 10#$HY2_BACKUP_PORT > 65535)); then
    stop "HY2_BACKUP_PORT is not a UDP port"
  fi
  HY2_BACKUP_PORT="$((10#$HY2_BACKUP_PORT))"
  [[ "$HY2_BACKUP_PORT" != 39051 && "$HY2_BACKUP_PORT" != 44877 ]] \
    || stop "HY2_BACKUP_PORT $HY2_BACKUP_PORT belongs to AmneziaWG"
  [[ "$HY2_BACKUP_PORT" != "$HY2_PORT" ]] || stop "HY2_BACKUP_PORT is the same as HY2_PORT"
fi
[[ "$CONNTRACK_MAX" =~ ^[0-9]{4,8}$ ]] || stop "CONNTRACK_MAX is not a number of entries"
# 2096 and 2098 are the old Marzban ports: their rules are in OBSOLETE below and
# would be removed again right after being added.
[[ "$((10#$DE_PORT))" != 2096 && "$((10#$DE_PORT))" != 2098 ]] \
  || stop "DE_PORT $DE_PORT is an old Marzban port; the Hiddify entry is 2443"

# table, chain, how to add (A = append, I = insert first), rule
RULES=(
  "nat|PREROUTING|A|-p tcp --dport 443 -j DNAT --to-destination $DE_TUNNEL_IP:$DE_PORT"
  "nat|PREROUTING|A|-p tcp --dport 2087 -j DNAT --to-destination $DE_TUNNEL_IP:$DE_PORT"
  "nat|PREROUTING|A|-p tcp --dport 8443 -j DNAT --to-destination $DE_TUNNEL_IP:$DE_PORT"
  "nat|PREROUTING|A|-p tcp --dport 2083 -j DNAT --to-destination $DE_TUNNEL_IP:$DE_PORT"
  "nat|PREROUTING|A|-p tcp --dport 2085 -j DNAT --to-destination $DE_TUNNEL_IP:$DE_PORT"
  "nat|POSTROUTING|A|-d $DE_TUNNEL_IP -o $TUNNEL_IF -p tcp --dport $DE_PORT -j MASQUERADE"
  "mangle|FORWARD|A|-p tcp --tcp-flags SYN,RST SYN -j TCPMSS --clamp-mss-to-pmtu"
  "nat|PREROUTING|A|-p udp --dport $HY2_PORT -j DNAT --to-destination $DE_IP:$HY2_PORT"
  "nat|POSTROUTING|A|-d $DE_IP -p udp --dport $HY2_PORT -j MASQUERADE"
)
# The spare port lands on the same DE port, so the MASQUERADE above covers it.
if [[ -n "$HY2_BACKUP_PORT" ]]; then
  RULES+=("nat|PREROUTING|A|-p udp --dport $HY2_BACKUP_PORT -j DNAT --to-destination $DE_IP:$HY2_PORT")
fi

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
  # Direct TCP forwards over the open internet (16.11.2026, before wg-vpn).
  "nat|PREROUTING|-p tcp --dport 443 -j DNAT --to-destination $DE_IP:$DE_PORT"
  "nat|PREROUTING|-p tcp --dport 2087 -j DNAT --to-destination $DE_IP:$DE_PORT"
  "nat|PREROUTING|-p tcp --dport 8443 -j DNAT --to-destination $DE_IP:$DE_PORT"
  "nat|PREROUTING|-p tcp --dport 2083 -j DNAT --to-destination $DE_IP:$DE_PORT"
  "nat|PREROUTING|-p tcp --dport 2085 -j DNAT --to-destination $DE_IP:$DE_PORT"
  "nat|POSTROUTING|-d $DE_IP -p tcp --dport $DE_PORT -j MASQUERADE"
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
      # Runs inside $(...), where set -e does not apply: a failed delete must
      # end the loop explicitly, or it would spin on the same rule forever.
      # shellcheck disable=SC2086
      "$IPT" -t "$table" -D "$chain" $spec || return 1
      n=$((n + 1))
    done
  done
  printf '%s\n' "$n"
}

conntrack_text() {
  printf '%s\n' "# VPN relay on RU (scripts/ru_vless_relay.sh): room for NAT entries of many clients." \
    "net.netfilter.nf_conntrack_max = $CONNTRACK_MAX"
}

# Writes the sysctl file only when it differs, then sets the live value.
set_conntrack() {
  local tmp
  mkdir -p "$(dirname "$RELAY_SYSCTL_CONF")" || return 1
  if ! [[ -f "$RELAY_SYSCTL_CONF" ]] || ! cmp -s <(conntrack_text) "$RELAY_SYSCTL_CONF"; then
    tmp="$(mktemp "$RELAY_SYSCTL_CONF.XXXXXX")" || return 1
    conntrack_text >"$tmp" && chmod 644 "$tmp" && mv -f "$tmp" "$RELAY_SYSCTL_CONF" || return 1
  fi
  "$SYSCTL" -q -w "net.netfilter.nf_conntrack_max=$CONNTRACK_MAX" >/dev/null
}

case "$MODE" in
  --check)
    say "ip_forward: $("$SYSCTL" -n net.ipv4.ip_forward 2>/dev/null || echo unknown)"
    say "nf_conntrack_max: $("$SYSCTL" -n net.netfilter.nf_conntrack_max 2>/dev/null || echo unknown) (wanted $CONNTRACK_MAX)"
    show
    say "RU_VLESS_RELAY_CHECK_OK: nothing changed. Switch on with: --apply"
    ;;
  --apply)
    [[ "$("$SYSCTL" -n net.ipv4.ip_forward 2>/dev/null)" == 1 ]] \
      || stop "net.ipv4.ip_forward is not 1 on this server"
    "$IP_BIN" link show dev "$TUNNEL_IF" >/dev/null 2>&1 \
      || stop "no $TUNNEL_IF interface: start the tunnel first (systemctl enable --now wg-quick@$TUNNEL_IF); nothing changed"
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
        fi || stop "a rule could not be added; nothing saved. Undo: iptables-restore < the backup above"
        added=$((added + 1))
      done
      dropped="$(drop_obsolete)" \
        || stop "an obsolete rule could not be removed; nothing saved. Undo: iptables-restore < the backup above"
    fi
    "$SAVE" save >/dev/null 2>&1 || stop "rules are active but not saved: run $SAVE save"
    set_conntrack || stop "rules are saved, but nf_conntrack_max could not be set ($RELAY_SYSCTL_CONF)"
    show
    say "RU_VLESS_RELAY_OK: TCP 443/2087/8443/2083/2085 -> $DE_TUNNEL_IP:$DE_PORT via $TUNNEL_IF, UDP $HY2_PORT${HY2_BACKUP_PORT:+ and $HY2_BACKUP_PORT} -> $DE_IP:$HY2_PORT, conntrack max $CONNTRACK_MAX ($added rules added, $dropped obsolete removed, saved)"
    ;;
  --remove)
    removed=0
    if (($(present_count) > 0 || $(obsolete_count) > 0)); then
      backup_rules
      for entry in "${RULES[@]}"; do
        IFS='|' read -r table chain how spec <<<"$entry"
        while has_rule "$table" "$chain" "$spec"; do
          # shellcheck disable=SC2086
          "$IPT" -t "$table" -D "$chain" $spec \
            || stop "a rule could not be removed; nothing saved. Undo: iptables-restore < the backup above"
          removed=$((removed + 1))
        done
      done
      dropped="$(drop_obsolete)" \
        || stop "an obsolete rule could not be removed; nothing saved. Undo: iptables-restore < the backup above"
      removed=$((removed + dropped))
    fi
    "$SAVE" save >/dev/null 2>&1 || stop "rules are removed but not saved: run $SAVE save"
    say "RU_VLESS_RELAY_REMOVED: $removed rules removed, saved"
    ;;
  *)
    stop "usage: --check | --apply | --remove"
    ;;
esac
