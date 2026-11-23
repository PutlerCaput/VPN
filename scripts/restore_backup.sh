#!/usr/bin/env bash
# Verify an age-encrypted backup in a new private directory.
# Usage: bash scripts/restore_backup.sh <backup.tar.gz.age> <age-key.txt> [folder]
set -euo pipefail
umask 077

ARCHIVE="${1:-}"
IDENTITY="${2:-}"
DEST="${3:-${TMPDIR:-/tmp}/restore-$(date -u +%Y%m%dT%H%M%SZ)}"

fail() { printf 'RESTORE_FAILED: %s\n' "$*" >&2; exit 1; }

[[ -f "$ARCHIVE" && -f "$IDENTITY" ]] || fail "usage: restore_backup.sh <backup.tar.gz.age> <age-key.txt> [folder]"
command -v python3 >/dev/null 2>&1 || fail "Python 3 is required (in Termux: pkg install python)"
if age --version >/dev/null 2>&1; then
  OPEN=age
elif command -v rage >/dev/null 2>&1; then
  OPEN=rage
else
  fail "age is not installed or does not run (apt-get install age; in Termux: pkg install rage)"
fi

DEST="$(python3 -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).parent.resolve() / Path(sys.argv[1]).name)' "$DEST")"
[[ "$DEST" != */. && "$DEST" != */.. ]] || fail "choose a new destination directory"
dir="$(dirname -- "$DEST")"
while [[ "$dir" != / ]]; do
  [[ ! -e "$dir/.git" ]] || fail "the restore directory must be outside a Git checkout"
  dir="$(dirname -- "$dir")"
done
[[ ! -e "$DEST" && ! -L "$DEST" ]] || fail "$DEST already exists: choose a new folder"
mkdir -- "$DEST" || fail "cannot create restore directory"
trap 'rm -rf -- "$DEST"' EXIT

# Keep the decryption stream on FD 3 while the Python source uses stdin.
if ! "$OPEN" -d -i "$IDENTITY" -- "$ARCHIVE" 2>/dev/null |
    python3 - "$DEST" 3<&0 <<'PY'
import hashlib
import os
import sys
import tarfile
from pathlib import Path, PurePosixPath


def checksum_line(name, digest):
    encoded = os.fsencode("./" + name)
    escaped = encoded.replace(b"\\", b"\\\\").replace(b"\n", b"\\n").replace(b"\r", b"\\r")
    prefix = b"" if escaped == encoded else b"\\"
    return prefix + digest.hexdigest().encode("ascii") + b"  " + escaped


def unpack(stream, dest):
    checksums = set()
    paths = set()
    with tarfile.open(fileobj=stream, mode="r|gz") as archive:
        for member in archive:
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or path in paths:
                raise ValueError
            paths.add(path)
            if not path.parts and member.isdir():
                continue
            if path.parts[0:1] not in {("bot",), ("hiddify",)}:
                if str(path) not in {"INFO.txt", "MANIFEST.sha256"} or not member.isreg():
                    raise ValueError
            target = dest.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(exist_ok=True, parents=True)
                continue
            if not member.isreg():
                raise ValueError
            target.parent.mkdir(exist_ok=True, parents=True)
            digest = hashlib.sha256()
            source = archive.extractfile(member)
            if source is None:
                raise ValueError
            with source, target.open("xb") as output:
                while block := source.read(1024 * 1024):
                    output.write(block)
                    digest.update(block)
            if str(path) != "MANIFEST.sha256":
                checksums.add(checksum_line(str(path), digest))
    # The tar end marker can arrive before the authentication stream ends.
    while stream.read(1024 * 1024):
        pass
    manifest = (dest / "MANIFEST.sha256").read_bytes().split(b"\n")
    if manifest[-1] != b"" or sorted(manifest[:-1]) != sorted(checksums):
        raise ValueError
    if (dest / "bot/bot.db").stat().st_size == 0 or not (dest / "INFO.txt").is_file():
        raise ValueError


try:
    with os.fdopen(3, "rb") as stream:
        unpack(stream, Path(sys.argv[1]))
except (EOFError, OSError, ValueError, tarfile.TarError):
    sys.exit(1)
PY
then
  fail "decryption, extraction or manifest verification failed"
fi
trap - EXIT

printf 'RESTORE_OK in %s\n' "$DEST"
printf 'Archive contents verified. Verify the database and panel in an isolated environment.\n'
printf 'Follow docs/FOUR_NODE_MIGRATION_2026-11-20.md for recovery and cutover.\n'
