#!/usr/bin/env bash
# Key-only SSH for the server, with a way back if it goes wrong.
#
#   bash scripts/ssh_hardening.sh             # report: settings, keys, who logged in from where
#   bash scripts/ssh_hardening.sh --apply     # passwords off; undoes itself in 10 minutes
#   bash scripts/ssh_hardening.sh --confirm   # from a NEW key login: keep the change
#   bash scripts/ssh_hardening.sh --revert    # take the change back at once
#   bash scripts/ssh_hardening.sh --status    # one line for the doctor: OK or WARN
#   bash scripts/ssh_hardening.sh --remove-key <fingerprint start>   # delete a key that is not yours
#
# The report numbers the keys in authorized_keys and ties every login by key to
# the key it used (sshd logs the fingerprint), with the address and the last
# time. A key nobody of yours uses, or an address you do not know, shows here.
#
# --apply writes ONE drop-in file, 00-cyberdefency-hardening.conf. sshd keeps the
# first value it reads, so a file that sorts first wins over the cloud image's
# later "PasswordAuthentication yes". It sets only what is looser than wanted and
# never loosens anything: no passwords (also not through PAM's keyboard-
# interactive), root only by key, no empty passwords, no X11.
#
# Why it cannot lock you out:
#   1. it refuses unless root's authorized_keys holds a key AND the log shows a
#      login by key in the last 14 days (so not when you are in through the
#      hosting console and have never tried the key);
#   2. `sshd -t` must accept the file and `sshd -T` must show the new values,
#      otherwise the file is removed again and nothing has changed;
#   3. a timer undoes the change after 10 minutes. It stays unless --confirm is
#      run from a NEW session that logged in by key AFTER the change;
#   4. the window you ran it in stays open: only new connections are affected.
# --remove-key keeps a dated copy of authorized_keys and never removes the last key.
#
# Not touched on purpose: the port and the firewall (the VPN depends on them), the
# host keys (a new one makes every client warn "HOST IDENTIFICATION HAS CHANGED"),
# MaxAuthTries and LoginGraceTime (a phone user typing a key passphrase, or a
# client holding several keys, would be refused), fail2ban (with passwords off a
# wrong guess gets nothing, and it is one more process on a small server).
set -euo pipefail

WORK=""
trap '[[ -z "$WORK" ]] || rm -rf "$WORK"' EXIT

usage() {
  echo "usage: ssh_hardening.sh [--apply | --confirm | --revert | --status | --remove-key <fingerprint start>]" >&2
  exit 2
}

MODE=report
KEY_ARG=""
case "${1:-}" in
  "") ;;
  --apply | --confirm | --revert | --status) MODE="${1#--}" ;;
  --remove-key)
    MODE=remove-key
    KEY_ARG="${2:-}"
    ;;
  *) usage ;;
esac
if [[ "$MODE" == remove-key ]]; then
  [[ $# -eq 2 ]] || usage
else
  [[ $# -le 1 ]] || usage
fi

SELF="$(readlink -f "$0")"
ETC_DIR="${ETC_DIR:-/etc}"
SSH_DIR="$ETC_DIR/ssh"
DROPIN_DIR="$SSH_DIR/sshd_config.d"
DROPIN="$DROPIN_DIR/00-cyberdefency-hardening.conf"
BEFORE="$DROPIN.before"      # an earlier version of the drop-in, kept until --confirm
PENDING="$SSH_DIR/.hardening-pending"  # exists from --apply until --confirm or --revert
AUTH_KEYS="${AUTH_KEYS:-/root/.ssh/authorized_keys}"
AUTH_LOG="${AUTH_LOG:-/var/log/auth.log}"
REVERT_AFTER="${REVERT_AFTER:-600}"    # seconds
LOOKBACK="${LOOKBACK:-14 days}"
UNIT=ssh-hardening-revert
PLAN=()
SSHD_T=""
LOG=""
KBD=kbdinteractiveauthentication

say() { printf '%s\n' "$*"; }
plan_text() { local IFS=';'; printf '%s' "${PLAN[*]}"; }
section() { printf '\n== %s\n' "$*"; }
stop() { printf 'SSH_HARDENING_STOPPED: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || stop "$1 is not installed"; }

[[ $EUID -eq 0 ]] || stop "run as root"
need sshd
need systemctl
SVC=ssh
systemctl cat ssh.service >/dev/null 2>&1 || SVC=sshd

load_effective() {
  SSHD_T="$(sshd -T 2>/dev/null || true)"
  KBD=kbdinteractiveauthentication  # OpenSSH before 8.7 calls it challengeresponseauthentication
  has_keyword "$KBD" || KBD=challengeresponseauthentication
}
effective() {  # effective <keyword>: the value sshd uses for new connections
  awk -v k="$1" '$1 == k && !seen { sub(/^[^ ]+ /, ""); print; seen = 1 }' <<<"$SSHD_T"
}
has_keyword() { grep -qi "^$1 " <<<"$SSHD_T"; }

plan() {  # PLAN: one "Keyword value" line per setting that is looser than wanted
  PLAN=()
  [[ "$(effective passwordauthentication)" == no ]] || PLAN+=("PasswordAuthentication no")
  if has_keyword "$KBD" && [[ "$(effective "$KBD")" != no ]]; then
    if [[ "$KBD" == kbdinteractiveauthentication ]]; then
      PLAN+=("KbdInteractiveAuthentication no")
    else
      PLAN+=("ChallengeResponseAuthentication no")
    fi
  fi
  [[ "$(effective permitrootlogin)" != yes ]] || PLAN+=("PermitRootLogin prohibit-password")
  [[ "$(effective permitemptypasswords)" != yes ]] || PLAN+=("PermitEmptyPasswords no")
  [[ "$(effective x11forwarding)" != yes ]] || PLAN+=("X11Forwarding no")
}

key_table() {  # one "<fingerprint> <TYPE> <comment>" line per key in authorized_keys
  [[ -r "$AUTH_KEYS" ]] || return 0
  # A comment is whatever whoever added the key wrote: no control characters
  # (a terminal would obey them), and no more than 30 characters.
  ssh-keygen -lf "$AUTH_KEYS" 2>/dev/null | tr -d '\000-\010\013-\037\177' | awk '
    { c = ""; for (i = 3; i < NF; i++) c = c (c == "" ? "" : " ") $i
      t = $NF; gsub(/[()]/, "", t); print $2, t, substr(c, 1, 30) }' || true
}

ssh_log() {  # sshd's login lines of the last LOOKBACK: the journal, else auth.log
  local out="" pattern='Accepted |Failed |Invalid user|authentication failure'
  if command -v journalctl >/dev/null 2>&1; then
    out="$(journalctl -u ssh -u sshd --since "$LOOKBACK ago" --no-pager -o short-iso 2>/dev/null |
      grep -E "$pattern" || true)"
  fi
  if [[ -z "$out" && -r "$AUTH_LOG" ]]; then
    out="$(grep -E "sshd(-session)?\[[0-9]+\]: .*($pattern)" "$AUTH_LOG" 2>/dev/null || true)"
  fi
  printf '%s\n' "$out"
}

logins() {  # logins <method>: "<time> <user> <address> <key fingerprint>" per successful login
  grep -E "Accepted $1 for " <<<"$LOG" | awk '
    { ts = ($1 ~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T/) ? substr($1, 1, 16) : "-"
      for (i = 1; i < NF; i++) { if ($i == "for") user = $(i + 1); if ($i == "from") addr = $(i + 1) }
      print ts, user, addr, ($NF ~ /^SHA256:/) ? $NF : "-" }' || true
}

summarize() {  # "<time> <user> <address> <fp>" in, "<n> <user> <address> <fp> <last time>" out
  awk 'NF >= 4 { k = $2 " " $3 " " $4; n[k]++; if ($1 > last[k]) last[k] = $1 }
       END { for (k in n) print n[k], k, last[k] }' | sort -rn
}

when() {  # 2026-11-08T12:01 -> "2026-11-08 12:01"; "-" (no time in the log) -> nothing
  [[ "$1" == - || -z "$1" ]] || printf '%s' "${1/T/ }"
}

undo_files() {
  if [[ -f "$BEFORE" ]]; then mv -f -- "$BEFORE" "$DROPIN"; else rm -f -- "$DROPIN"; fi
}

reload_sshd() { systemctl try-reload-or-restart "$SVC"; }

do_revert() {
  undo_files
  rm -f -- "$PENDING"
  if sshd -t 2>/dev/null; then reload_sshd >/dev/null 2>&1 || true; fi
  systemctl stop "$UNIT.timer" >/dev/null 2>&1 || true
}

show_rows() {  # show_rows <summarized rows> <limit> <key|pass>; uses keyno from the caller
  local n user addr fp last shown=0 total tag
  total="$(grep -c . <<<"$1" || true)"
  while read -r n user addr fp last; do
    [[ -n "$n" ]] || continue
    shown=$((shown + 1))
    ((shown <= $2)) || break
    tag=""
    if [[ "$3" == key && "$fp" == - ]]; then
      tag="  (the log does not name the key)"
    elif [[ "$3" == key && -n "${keyno[$fp]:-}" ]]; then
      tag="  key #${keyno[$fp]}"
    elif [[ "$3" == key ]]; then
      tag="  key NOT in the list (removed, or from another file)"
    fi
    printf '    %4sx  %s from %s%s  last %s\n' "$n" "$user" "$addr" "$tag" "$(when "$last")"
  done <<<"$1"
  ((total <= $2)) || printf '    ... and %d more rows\n' $((total - $2))
}

listening() {  # "public <ports>" and "private <ports>": what listens beyond loopback
  ss -H -lnt 2>/dev/null | awk '
    { a = $4; p = a; sub(/.*:/, "", p); sub(/:[0-9]+$/, "", a); gsub(/[][]/, "", a); sub(/%.*/, "", a)
      if (a ~ /^127\./ || a == "::1") next
      if (a ~ /^10\./ || a ~ /^192\.168\./ || a ~ /^172\.(1[6-9]|2[0-9]|3[01])\./ || a ~ /^f[cd][0-9a-f][0-9a-f]:/ || a ~ /^fe80:/) priv[p] = 1
      else pub[p] = 1 }
    END { printf "public"; for (p in pub) printf " %s", p; printf "\n"
          printf "private"; for (p in priv) if (!(p in pub)) printf " %s", p; printf "\n" }' || true
}

report() {
  need ssh-keygen
  load_effective
  [[ -n "$SSHD_T" ]] || stop "sshd -T gave no answer: is the OpenSSH server installed?"
  LOG="$(ssh_log)"
  plan
  say "SSH hardening: REPORT ONLY (nothing is changed)"

  section "Settings for new connections"
  local rows=(
    "password login|passwordauthentication|no"
    "keyboard-interactive (PAM)|$KBD|no"
    "empty passwords|permitemptypasswords|no"
    "X11 forwarding|x11forwarding|no"
  )
  local row label keyword wanted now
  for row in "${rows[@]}"; do
    IFS='|' read -r label keyword wanted <<<"$row"
    now="$(effective "$keyword")"
    if [[ "$now" == "$wanted" ]]; then
      printf '  %-28s %-20s ok\n' "$label" "$now"
    else
      printf '  %-28s %-20s -> %s\n' "$label" "${now:-unknown}" "$wanted"
    fi
  done
  now="$(effective permitrootlogin)"
  if [[ "$now" == yes ]]; then
    printf '  %-28s %-20s -> prohibit-password\n' "root login" "$now"
  else
    printf '  %-28s %-20s ok (root by key only, or stricter)\n' "root login" "${now:-unknown}"
  fi
  if grep -rEiq '^[[:space:]]*Match[[:space:]]' "$SSH_DIR/sshd_config" "$DROPIN_DIR" 2>/dev/null; then
    say "  NOTE: 'Match' blocks exist in $SSH_DIR; they can override these settings for some users or addresses. Read them before relying on this."
  fi

  local by_key by_pass key_rows pass_rows n_key n_pass
  by_key="$(logins publickey)"
  by_pass="$(logins password)"
  key_rows="$(summarize <<<"$by_key")"
  pass_rows="$(summarize <<<"$by_pass")"
  n_key="$(awk '{ s += $1 } END { print s + 0 }' <<<"$key_rows")"
  n_pass="$(awk '{ s += $1 } END { print s + 0 }' <<<"$pass_rows")"

  section "Keys that can log in ($AUTH_KEYS)"
  local keys fp type comment i=0 used last
  declare -A keyno=()
  keys="$(key_table)"
  if [[ -z "$keys" ]]; then
    say "  none: with passwords off nobody could log in, so --apply refuses"
  else
    while read -r fp type comment; do
      i=$((i + 1))
      keyno[$fp]=$i
      read -r used last < <(awk -v fp="$fp" '$4 == fp { n += $1; if ($5 > l) l = $5 } END { print n + 0, l }' <<<"$key_rows")
      printf '  #%d %s "%s"\n     %s\n' "$i" "$type" "$comment" "$fp"
      if ((used == 0)); then
        say "     no logins in the last $LOOKBACK"
      else
        printf '     %d logins in the last %s, last %s\n' "$used" "$LOOKBACK" "$(when "$last")"
      fi
    done <<<"$keys"
    say "  Yours? Phone: ssh-keygen -lf ~/.ssh/id_ed25519.pub   PC (PowerShell): ssh-keygen -lf \$env:USERPROFILE\\.ssh\\id_ed25519.pub"
    say "  Not yours: bash scripts/ssh_hardening.sh --remove-key <first 6+ characters after SHA256:>"
  fi

  section "Logins in the last $LOOKBACK (check that every address is yours)"
  say "  by key:      $n_key"
  show_rows "$key_rows" 12 key
  say "  by password: $n_pass  (these stop working after --apply)"
  show_rows "$pass_rows" 8 pass
  local bad_lines bad_addrs
  bad_lines="$(grep -cE 'Failed password|Invalid user|authentication failure' <<<"$LOG" || true)"
  bad_addrs="$(grep -oE '(Invalid user [^ ]* from|Failed password for .* from) [0-9a-fA-F:.]+' <<<"$LOG" | awk '{ print $NF }' | sort -u | grep -c . || true)"
  say "  refused guesses: $bad_lines log lines from $bad_addrs addresses"
  say "  An address you do not know: open ipinfo.io/<address> in a browser to see who owns it."

  section "Listening on this server (the firewall decides who can reach it; this script never touches it)"
  local listen pub priv
  listen="$(listening)"
  pub="$(awk '$1 == "public" { for (i = 2; i <= NF; i++) print $i }' <<<"$listen" | sort -un | paste -sd' ' -)"
  priv="$(awk '$1 == "private" { for (i = 2; i <= NF; i++) print $i }' <<<"$listen" | sort -un | paste -sd' ' -)"
  say "  on every address: ${pub:-none or unknown}"
  say "  on a private address only: ${priv:-none}"

  section "What --apply would do"
  if [[ -f "$PENDING" ]]; then
    say "  A change is waiting: run --confirm from a new key login, or --revert."
  elif ((${#PLAN[@]} == 0)); then
    say "  Nothing: SSH is already as strict as this script makes it."
  else
    printf '  set: %s\n' "${PLAN[@]}"
    if [[ -n "$keys" ]]; then
      say "  keys:   ready"
    else
      say "  keys:   NOT ready (no key in $AUTH_KEYS)"
    fi
    if [[ -n "$by_key" ]]; then
      say "  log:    ready (a login by key was seen)"
    else
      say "  log:    NOT ready (no login by key in the last $LOOKBACK: log in with your key once, from Termux, then run again)"
    fi
    say "  then a timer undoes it after $((REVERT_AFTER / 60)) minutes unless you run --confirm from a NEW key login."
  fi
  say "SSH_HARDENING_REPORT_DONE"
}

apply() {
  [[ ! -f "$PENDING" ]] || stop "a change is already waiting: run --confirm from a new key login, or --revert"
  need ssh-keygen
  need systemd-run
  load_effective
  [[ -n "$SSHD_T" ]] || stop "sshd -T gave no answer: is the OpenSSH server installed?"
  plan
  if ((${#PLAN[@]} == 0)); then
    say "SSH_HARDENING_NOTHING_TO_DO: SSH is already as strict as this script makes it."
    exit 0
  fi
  [[ -n "$(key_table)" ]] || stop "no key in $AUTH_KEYS; with passwords off nobody could log in. Add your key first (docs/SERVER_MAINTENANCE.md)"
  LOG="$(ssh_log)"
  [[ -n "$(logins publickey)" ]] \
    || stop "the log shows no login by key in the last $LOOKBACK. Log in from Termux with your key, then run this again from that session"

  mkdir -p "$DROPIN_DIR"
  if [[ -f "$DROPIN" ]]; then cp -p -- "$DROPIN" "$BEFORE"; fi
  # An earlier run's settings stay, unless this run sets the same keyword again.
  local kept=() line word new key replaced
  if [[ -f "$BEFORE" ]]; then
    while IFS= read -r line; do
      [[ "$line" =~ ^[[:space:]]*(#|$) ]] && continue
      word="${line%% *}"
      replaced=0
      for new in "${PLAN[@]}"; do
        key="${new%% *}"
        [[ "${key,,}" == "${word,,}" ]] && replaced=1
      done
      ((replaced)) || kept+=("$line")
    done <"$BEFORE"
  fi
  {
    printf '# Written by scripts/ssh_hardening.sh. Undo: bash scripts/ssh_hardening.sh --revert\n'
    printf '%s\n' "${kept[@]+"${kept[@]}"}" "${PLAN[@]}"
  } >"$DROPIN"
  chmod 644 "$DROPIN"

  if ! sshd -t 2>/dev/null; then
    undo_files
    stop "sshd rejected the new settings; nothing changed"
  fi
  load_effective
  plan
  if ((${#PLAN[@]} > 0)); then
    undo_files
    stop "the new settings are not in force: another file sets these first: $(plan_text). Find it: grep -ri passwordauthentication $SSH_DIR. Nothing changed"
  fi

  date +%s >"$PENDING"
  if ! systemd-run --quiet --unit="$UNIT" --on-active="${REVERT_AFTER}s" \
    --description="Undo the SSH hardening unless it was confirmed" /bin/bash "$SELF" --revert; then
    rm -f -- "$PENDING"
    undo_files
    stop "the safety timer could not be started; nothing changed"
  fi
  if ! reload_sshd; then
    do_revert
    stop "sshd did not reload; the old settings are back"
  fi

  say "SSH_HARDENING_APPLIED: new connections can no longer use a password."
  say "It undoes itself in $((REVERT_AFTER / 60)) minutes unless you confirm. Keep THIS window open and:"
  say "  1. open a NEW session with your key (Termux: a new window, the usual ssh command);"
  say "  2. if it lets you in, run there:  cd /root/vpn-src && bash scripts/ssh_hardening.sh --confirm"
  say "If the new session does not let you in, do nothing: the old settings come back by themselves."
  say "To take it back at once, run here: bash scripts/ssh_hardening.sh --revert"
}

confirm() {
  [[ -f "$PENDING" ]] || stop "no change is waiting; nothing to confirm"
  local since fresh
  since="$(cat "$PENDING")"
  [[ "$since" =~ ^[0-9]+$ ]] || stop "$PENDING is damaged: run --revert"
  need journalctl
  fresh="$(journalctl -u ssh -u sshd --since "@$since" --no-pager -o cat 2>/dev/null | grep -c 'Accepted publickey' || true)"
  ((fresh >= 1)) \
    || stop "no login by key since the change. Open a NEW session with your key and run --confirm there (this window does not prove the key works)"
  load_effective
  plan
  if ((${#PLAN[@]} > 0)); then
    stop "the settings are no longer in force ($(plan_text)); run --revert and look at $SSH_DIR"
  fi
  systemctl stop "$UNIT.timer" >/dev/null 2>&1 || true
  rm -f -- "$PENDING" "$BEFORE"
  say "SSH_HARDENING_OK: passwords are off for SSH, keys only. The timer is stopped."
  say "Undo any time: bash scripts/ssh_hardening.sh --revert"
}

status() {
  load_effective
  if [[ -z "$SSHD_T" ]]; then
    say "WARN ssh: the settings could not be read (run as root; is the OpenSSH server installed?)"
    return 0
  fi
  if [[ -f "$PENDING" ]]; then
    say "WARN ssh: a change waits for confirmation and undoes itself: bash scripts/ssh_hardening.sh --confirm"
    return 0
  fi
  if [[ "$(effective passwordauthentication)" != no || "$(effective "$KBD")" != no ]]; then
    say "WARN ssh: password login is on, keys only is safer: bash scripts/ssh_hardening.sh"
    return 0
  fi
  say "OK ssh: password login is off, keys only"
}

remove_key() {  # remove_key: delete the key whose fingerprint starts with $KEY_ARG
  local want="${KEY_ARG#SHA256:}" tmp line fp type comment total=0 gone=0 backup
  local gone_fp="" gone_type="" gone_comment="" matched=()
  [[ "$want" =~ ^[A-Za-z0-9+/]{6,}$ ]] \
    || stop "give at least 6 characters of the fingerprint, as printed after SHA256: in the report"
  [[ -f "$AUTH_KEYS" ]] || stop "$AUTH_KEYS does not exist"
  need ssh-keygen
  tmp="$(mktemp -d)"
  WORK="$tmp"
  : >"$tmp/keep"
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ ^[[:space:]]*(#|$) ]]; then
      printf '%s\n' "$line" >>"$tmp/keep"
      continue
    fi
    printf '%s\n' "$line" >"$tmp/one"
    fp="" type="" comment=""
    read -r fp type comment < <(ssh-keygen -lf "$tmp/one" 2>/dev/null | tr -d '\000-\010\013-\037\177' | awk '
      NR == 1 { c = ""; for (i = 3; i < NF; i++) c = c (c == "" ? "" : " ") $i
                t = $NF; gsub(/[()]/, "", t); print $2, t, substr(c, 1, 30) }' || true) || true
    if [[ -z "${fp:-}" ]]; then  # not a key line sshd would read: left as it is
      printf '%s\n' "$line" >>"$tmp/keep"
      continue
    fi
    total=$((total + 1))
    if [[ "${fp#SHA256:}" == "$want"* ]]; then
      gone=$((gone + 1))
      matched+=("$fp")
      gone_fp="$fp"
      gone_type="$type"
      gone_comment="$comment"
    else
      printf '%s\n' "$line" >>"$tmp/keep"
    fi
  done <"$AUTH_KEYS"

  ((gone > 0)) || stop "no key in $AUTH_KEYS starts with $want; nothing changed"
  if (($(printf '%s\n' "${matched[@]}" | sort -u | grep -c .) > 1)); then
    stop "more than one key starts with $want: give more characters; nothing changed"
  fi
  ((total - gone >= 1)) \
    || stop "that is the last key in $AUTH_KEYS; with none, nobody could log in by key; nothing changed"

  LOG="$(ssh_log)"
  local used last
  read -r used last < <(logins publickey | awk -v fp="$gone_fp" '$4 == fp { n++; if ($1 > l) l = $1 } END { print n + 0, l }')
  backup="$AUTH_KEYS.removed-$(date +%Y%m%dT%H%M%S)"
  cp -p -- "$AUTH_KEYS" "$backup"
  cat "$tmp/keep" >"$AUTH_KEYS"
  say "SSH_KEY_REMOVED: $gone_type \"$gone_comment\" $gone_fp"
  if ((used > 0)); then
    say "NOTE: this key had $used logins in the last $LOOKBACK, last $(when "$last"). If that was you, put the line back from the copy below."
  fi
  say "A dated copy of the old file: $backup"
  say "Put a key back by mistake-removal: open the hosting console and append its line from that copy to $AUTH_KEYS."
}

case "$MODE" in
  report) report ;;
  apply) apply ;;
  confirm) confirm ;;
  revert)
    if [[ ! -e "$DROPIN" && ! -e "$BEFORE" && ! -e "$PENDING" ]]; then
      say "SSH_HARDENING_REVERTED: nothing to undo."
    else
      do_revert
      say "SSH_HARDENING_REVERTED: the settings written by this script are removed; sshd reloaded."
    fi
    ;;
  status) status ;;
  remove-key) remove_key ;;
esac
