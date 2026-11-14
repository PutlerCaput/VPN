#!/usr/bin/env bash
# Signed releases: the server takes new code only when the owner signed it.
#
# Without this, whoever gets into the GitHub account (or the mailbox that resets
# its password, or a session with push rights) can put code on the servers: the
# next update installs it. With it, a release is a commit the owner signed with
# their own SSH key, on their phone; the server checks the signature against the
# owner's public key kept on the server, and refuses anything else.
#
# Installed once, from code already on the server, OUTSIDE the repository, so that
# new code cannot replace the check before it is checked:
#   bash scripts/release_guard.sh --install /root/owner_release_key.pub
#
# Then, on the server (as /usr/local/sbin/vpn-release-guard):
#   --fetch                fetch origin/main, show the commit to sign and what is new
#   --apply                read the owner's signature on standard input; if it is
#                          good for that commit, move the checkout to it
#   --status               one line: is the checkout a signed release?
#   --verify-head [DIR]    exit 0 if DIR's checkout is the signed release (the
#                          update scripts call this and refuse otherwise)
#
# The phone side is scripts/release_sign.sh. docs/SIGNED_RELEASES.md explains it.
set -euo pipefail

REPO="${RELEASE_REPO:-/root/vpn-src}"
ETC="${RELEASE_ETC:-/etc/vpn-release}"
BIN="${RELEASE_BIN:-/usr/local/sbin/vpn-release-guard}"
SIGNERS="$ETC/allowed_signers"
CURRENT="$ETC/current"
NAMESPACE="vpn-release"
BRANCH="${RELEASE_BRANCH:-main}"

say() { printf '%s\n' "$*"; }
stop() {
  printf 'RELEASE_STOPPED: %s\n' "$*" >&2
  exit 1
}

# The text the owner signs: the namespace and the full commit id, nothing else.
message() { printf 'vpn-release\n%s\n' "$1"; }

candidate() {
  git -C "$REPO" rev-parse --verify -q "refs/remotes/origin/$BRANCH^{commit}" \
    || stop "no origin/$BRANCH in $REPO: run --fetch first"
}

case "${1:-}" in
  --install)
    key="${2:-}"
    [[ -r "$key" ]] || stop "usage: --install /path/to/owner_key.pub"
    line="$(head -1 "$key")"
    [[ "$line" =~ ^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|sk-ssh-ed25519@openssh\.com)\ [A-Za-z0-9+/=]+ ]] \
      || stop "$key is not an SSH public key (one line: ssh-ed25519 AAAA...)"
    git -C "$REPO" rev-parse --verify -q HEAD >/dev/null || stop "$REPO is not a git checkout"
    install -d -m 0700 "$ETC"
    printf 'owner namespaces="%s" %s\n' "$NAMESPACE" "$line" >"$SIGNERS.new"
    chmod 0600 "$SIGNERS.new"
    mv "$SIGNERS.new" "$SIGNERS"
    # The code on the server now is the trusted starting point.
    git -C "$REPO" rev-parse HEAD >"$CURRENT"
    install -m 0755 "$(readlink -f "$0")" "$BIN"
    say "RELEASE_GUARD_INSTALLED: key $(ssh-keygen -lf "$SIGNERS" 2>/dev/null | awk '{print $2}' || echo set)"
    say "trusted now: $(git -C "$REPO" log -1 --format='%h %s')"
    ;;

  --fetch)
    [[ -r "$SIGNERS" ]] || stop "not installed: --install first"
    git -C "$REPO" fetch -q origin "$BRANCH"
    sha="$(candidate)"
    trusted="$(cat "$CURRENT")"
    say "RELEASE_CANDIDATE $sha"
    if [[ "$sha" == "$trusted" ]]; then
      say "nothing new: the server already runs this signed release"
      exit 0
    fi
    git -C "$REPO" merge-base --is-ancestor "$trusted" "$sha" \
      || say "WARNING: this commit does not continue the release in use (history was rewritten?). Do not sign it unless you know why."
    say "new since the release in use ($(git -C "$REPO" rev-list --count "$trusted..$sha") commits):"
    git -C "$REPO" log --format='  %h %s' "$trusted..$sha" | head -40
    ;;

  --apply)
    [[ -r "$SIGNERS" ]] || stop "not installed: --install first"
    sha="$(candidate)"
    sig="$(mktemp)"
    trap 'rm -f "$sig"' EXIT
    cat >"$sig"
    [[ -s "$sig" ]] || stop "no signature on standard input"
    message "$sha" | ssh-keygen -Y verify -f "$SIGNERS" -I owner -n "$NAMESPACE" -s "$sig" >/dev/null 2>&1 \
      || stop "the signature is not the owner's for $sha: nothing changed"
    trusted="$(cat "$CURRENT")"
    git -C "$REPO" merge-base --is-ancestor "$trusted" "$sha" \
      || stop "$sha does not continue the release in use; refusing to go back or sideways"
    git -C "$REPO" merge -q --ff-only "$sha" || stop "the checkout could not move to $sha"
    printf '%s\n' "$sha" >"$CURRENT"
    say "RELEASE_OK: $(git -C "$REPO" log -1 --format='%h %s')"
    ;;

  --status)
    if [[ ! -r "$SIGNERS" ]]; then
      say "WARN release guard: not installed (any pushed code can be deployed)"
      exit 0
    fi
    if [[ "$(git -C "$REPO" rev-parse HEAD)" == "$(cat "$CURRENT")" ]]; then
      say "OK release guard: the checkout is the signed release $(git -C "$REPO" log -1 --format=%h)"
    else
      say "WARN release guard: the checkout is NOT the signed release (someone ran git pull?)"
    fi
    ;;

  --verify-head)
    dir="${2:-$REPO}"
    [[ -r "$SIGNERS" ]] || exit 0   # not installed: nothing to enforce
    head="$(git -C "$dir" rev-parse HEAD 2>/dev/null || true)"
    [[ -n "$head" && "$head" == "$(cat "$CURRENT")" ]] \
      || stop "$dir is not the signed release. Update only with: vpn-release-guard --fetch, then sign on the phone (docs/SIGNED_RELEASES.md)"
    # Uncommitted edits in the checkout are not signed either.
    git -C "$dir" diff --quiet HEAD -- || stop "$dir has changes nobody signed"
    ;;

  *)
    stop "usage: --install KEY.pub | --fetch | --apply | --status | --verify-head [DIR]"
    ;;
esac
