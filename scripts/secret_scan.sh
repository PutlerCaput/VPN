#!/usr/bin/env bash
# Fails if a file assigns a literal value to password=/token=/secret=/key=.
# Allowed: empty values, ${VAR} references, file paths, code references.
# Usage: scripts/secret_scan.sh [file ...]   (no args: project text files, excluding local dependencies and runtime data)
set -euo pipefail

PATTERN='(?i)(password|token|secret|key)=(?:"(?!\$\{|/)[^"\s]{6,}"|'"'"'(?!\$\{|/)[^'"'"'\s]{6,}'"'"'|(?![$/{"'"'"'])[A-Za-z0-9_:+/.\-]*[0-9][A-Za-z0-9_:+/.\-]{10,}(?=[\s,;]|$))'

if [[ $# -eq 0 ]]; then
  cd "$(dirname "$0")/.."
  # A Docker inspect dump contains unredacted credentials even if the project
  # source itself is clean. Reject these tracked files before scanning content.
  tracked_sensitive="$(git ls-files | grep -E '(^|/)(docker-inspect[^/]*\.json|vpn-project-backup[^/]*\.(tar\.gz|age)|\.env($|\.before-|\.bak-)|age-key\.txt|AGE-SECRET-KEY-[^/]+)|(^|/)(secrets|diagnostics|server-backups)/' || true)"
  if [[ -n "$tracked_sensitive" ]]; then
    printf 'PRIVATE RUNTIME DUMP TRACKED (filenames only):\n%s\n' "$tracked_sensitive" >&2
    exit 1
  fi
  mapfile -t files < <(find . -type f \
    -not -path './.venv/*' -not -path './venv/*' -not -path './.pytest_cache/*' \
    -not -path './.git/*' -not -path '*/node_modules/*' -not -path '*/dist/*' \
    -not -path './secrets/*' -not -path './data/*' -not -path './logs/*' -not -path './backups/*' \
    -not -path '*/__pycache__/*' -not -path './.mypy_cache/*' -not -path './.ruff_cache/*' \
    -not -name 'package-lock.json' -not -name '.env')
  set -- "${files[@]}"
fi

if matches="$(grep -HnIP "$PATTERN" "$@" 2>/dev/null)"; then
  # Print only file:line, never the matched value.
  echo "$matches" | cut -d: -f1,2 | sed 's/^/  /' >&2
  echo "SECRET LEAK DETECTED" >&2
  exit 1
fi
echo "secret scan: clean"
