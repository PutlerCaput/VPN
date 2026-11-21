#!/usr/bin/env bash
# Same policy for CI and pre-commit; never print matched secret values.
set -euo pipefail
exec python3 "$(dirname "$0")/secret_scan.py" "$@"
