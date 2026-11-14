#!/usr/bin/env bash
# The owner's side of a signed release, run in Termux on the phone.
#
#   bash release_sign.sh            fetch on DE, show what is new, ask, sign, apply
#   bash release_sign.sh KEYFILE    the same with another private key
#
# The private key never leaves the phone: only the signature goes to the server.
# Sign only when the commit and the list of changes are what you expect (the
# commit id is the one Claude or GitHub named after the merge).
set -euo pipefail

KEY="${1:-$HOME/.ssh/id_ed25519}"
HOST="${RELEASE_HOST:-de}"
GUARD="vpn-release-guard"

stop() {
  printf 'RELEASE_SIGN_STOPPED: %s\n' "$*" >&2
  exit 1
}

[[ -r "$KEY" ]] || stop "no private key at $KEY"
# shellcheck disable=SC2029
out="$(ssh "$HOST" "$GUARD --fetch")" || stop "the server could not fetch"
printf '%s\n' "$out"
sha="$(awk '/^RELEASE_CANDIDATE /{print $2}' <<<"$out")"
[[ "$sha" =~ ^[0-9a-f]{40}$ ]] || stop "no commit to sign"
grep -q "nothing new" <<<"$out" && exit 0

printf '\nSign %s? Type the first 7 characters of the commit to confirm: ' "$sha"
IFS= read -r answer
[[ "$answer" == "${sha:0:7}" ]] || stop "not confirmed; nothing was signed"

sig="$(printf 'vpn-release\n%s\n' "$sha" | ssh-keygen -Y sign -f "$KEY" -n vpn-release)" \
  || stop "signing failed"
# shellcheck disable=SC2029
printf '%s\n' "$sig" | ssh "$HOST" "$GUARD --apply"
