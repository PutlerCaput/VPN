#!/usr/bin/env bash
# Read-only acceptance checks. No login, messages, invoices or account changes.
set -euo pipefail

web_origin="${1:-http://127.0.0.1:18082}"
case "$web_origin" in
  http://127.0.0.1:18082|https://app.cyberdefency.com) ;;
  *) printf 'Use the local web origin or https://app.cyberdefency.com\n' >&2; exit 2 ;;
esac

check_status() {
  local path="$1" expected="$2" actual
  actual="$(curl --noproxy 127.0.0.1,localhost --silent --show-error --max-time 10 --output /dev/null \
    --write-out '%{http_code}' "$web_origin$path")"
  if [[ "$actual" != "$expected" ]]; then
    printf '%s: expected %s, received %s\n' "$path" "$expected" "$actual" >&2
    return 1
  fi
  printf '%s HTTP %s\n' "$path" "$actual"
}

check_status / 200
check_status /tma/ 200
check_status /oferta 200
check_status /metrics 404
check_status /internal/abuse 404
check_status /api/v1/me 401

curl --noproxy 127.0.0.1,localhost --silent --show-error --fail --max-time 10 "$web_origin/healthz" | \
  python3 -c 'import json, sys; data=json.load(sys.stdin); sys.exit(0 if data.get("status")=="ok" and data.get("db") is True else 1)'
printf 'WEB_AND_DATABASE_CHECK_OK\n'
