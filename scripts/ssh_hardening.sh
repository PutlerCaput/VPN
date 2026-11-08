#!/usr/bin/env bash
# Key-only SSH for the server, with a way back if it goes wrong.
#
#   bash scripts/ssh_hardening.sh             # report: what is set now, what would change
#   bash scripts/ssh_hardening.sh --apply     # passwords off; undoes itself in 10 minutes
#   bash scripts/ssh_hardening.sh --confirm   # from a NEW key login: keep the change
#   bash scripts/ssh_hardening.sh --revert    # take the change back at once
#   bash scripts/ssh_hardening.sh --status    # one line for the doctor: OK or WARN
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
#
# Not touched on purpose: the port and the firewall (the VPN depends on them), the
# host keys (a new one makes every client warn "HOST IDENTIFICATION HAS CHANGED"),
# MaxAuthTries and LoginGraceTime (a phone user typing a key passphrase, or a
# client holding several keys, would be refused), fail2ban (with passwords off a
# wrong guess gets nothing, and it is one more process on a small server).
set -euo pipefail

MODE=report
case "${1:-}" in
  "") ;;
  --apply) MODE=apply ;;
  --confirm) MODE=confirm ;;
  --revert) MODE=revert ;;
  --status) MODE=status ;;
  *) MODE=usage ;;
esac
if [[ "$MODE" == usage || $# -gt 1 ]]; then
  echo "usage: ssh_hardening.sh [--apply | --confirm | --revert | --status]" >&2
  exit 2
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

key_lines() {  # "(TYPE) fingerprint" per key; the comment (often an e-mail) is left out
  [[ -r "$AUTH_KEYS" ]] || return 0
  ssh-keygen -lf "$AUTH_KEYS" 2>/dev/null | awk '{ print $NF, $2 }' || true
}

ssh_log() {  # sshd's login lines of the last LOOKBACK: the journal, else auth.log
  local out="" pattern='Accepted |Failed |Invalid user|authentication failure'
  if command -v journalctl >/dev/null 2>&1; then
    out="$(journalctl -u ssh -u sshd --since "$LOOKBACK ago" --no-pager -o cat 2>/dev/null |
      grep -E "$pattern" || true)"
  fi
  if [[ -z "$out" && -r "$AUTH_LOG" ]]; then
    out="$(grep -E "sshd(-session)?\[[0-9]+\]: .*($pattern)" "$AUTH_LOG" 2>/dev/null || true)"
  fi
  printf '%s\n' "$out"
}

accepted() {  # accepted <method>: "<user> <address>" per successful login
  grep -oE "Accepted $1 for [^ ]+ from [^ ]+" <<<"$LOG" | awk '{ print $4, $6 }' || true
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

  section "Keys that can log in ($AUTH_KEYS)"
  local keys
  keys="$(key_lines)"
  if [[ -z "$keys" ]]; then
    say "  none: with passwords off nobody could log in, so --apply refuses"
  else
    awk '{ print "  " $0 }' <<<"$keys"
    say "  Compare with your phone: ssh-keygen -lf ~/.ssh/id_ed25519.pub"
  fi

  section "Logins in the last $LOOKBACK (check that every address is yours)"
  local by_key by_pass bad_lines bad_addrs
  by_key="$(accepted publickey)"
  by_pass="$(accepted password)"
  say "  by key:      $(grep -c . <<<"$by_key" || true)"
  [[ -z "$by_key" ]] || sort <<<"$by_key" | uniq -c | sort -rn | awk 'NR <= 6 { print "    " $2 " from " $3 "  x" $1 }'
  say "  by password: $(grep -c . <<<"$by_pass" || true)  (these stop working after --apply)"
  [[ -z "$by_pass" ]] || sort <<<"$by_pass" | uniq -c | sort -rn | awk 'NR <= 6 { print "    " $2 " from " $3 "  x" $1 }'
  bad_lines="$(grep -cE 'Failed password|Invalid user|authentication failure' <<<"$LOG" || true)"
  bad_addrs="$(grep -oE '(Invalid user [^ ]* from|Failed password for .* from) [0-9a-fA-F:.]+' <<<"$LOG" | awk '{ print $NF }' | sort -u | grep -c . || true)"
  say "  refused guesses: $bad_lines log lines from $bad_addrs addresses"

  section "Ports open to the internet (this script never touches the firewall)"
  local ports
  ports="$(ss -H -lnt 2>/dev/null | awk '{ print $4 }' | grep -vE '^(127\.|\[::1\]|::1)|%lo' | sed 's/.*://' | sort -un | tr '\n' ' ' || true)"
  say "  ${ports:-unknown}"

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
  [[ -n "$(key_lines)" ]] || stop "no key in $AUTH_KEYS; with passwords off nobody could log in. Add your key first (docs/SERVER_MAINTENANCE.md)"
  LOG="$(ssh_log)"
  [[ -n "$(accepted publickey)" ]] \
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
esac
