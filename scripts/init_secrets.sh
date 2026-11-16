#!/usr/bin/env bash
# Creates ./secrets/<name>.txt for Docker Secrets. Existing files are kept.
set -euo pipefail
cd "$(dirname "$0")/.."
umask 077
mkdir -p secrets

write_secret() {
  local name="$1" value="$2"
  local path="secrets/${name}.txt"
  if [[ -s "$path" ]]; then
    echo "• ${name}: exists, skipped"
    return
  fi
  printf '%s' "$value" > "$path"
  echo "• ${name}: written"
}

ask() {
  local prompt="$1" value
  read -r -s -p "${prompt}: " value
  echo >&2
  printf '%s' "$value"
}

write_secret jwt_secret "$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
# SQLCipher key. Losing it makes the database and its backups unreadable:
# keep an offline copy next to the GPG backup key.
write_secret db_key "$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

if [[ ! -s secrets/vapid_private.txt ]]; then
  pair="$(python3 scripts/gen_vapid.py)"
  write_secret vapid_private "$(grep '^VAPID_PRIVATE=' <<<"$pair" | cut -d= -f2-)"
  echo "  Add to .env: $(grep '^VAPID_PUBLIC=' <<<"$pair")"
fi

[[ -s secrets/bot_token.txt ]] || write_secret bot_token "$(ask 'Telegram bot token')"
[[ -s secrets/hiddify_api_key.txt ]] || write_secret hiddify_api_key "$(ask 'Hiddify Manager API key (admin UUID)')"
[[ -s secrets/crypto_pay_token.txt ]] || write_secret crypto_pay_token "$(ask 'Crypto Pay API token (empty to disable USDT)')"
[[ -s secrets/sbp_provider_token.txt ]] || write_secret sbp_provider_token "$(ask 'SBP aggregator HMAC secret (empty unless robokassa/payanyway)')"
[[ -s secrets/yookassa_secret_key.txt ]] || write_secret yookassa_secret_key "$(ask 'YooKassa secret key (empty if not using YooKassa)')"
[[ -s secrets/abuse_report_token.txt ]] || write_secret abuse_report_token "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"

# Empty = the local database. The file must exist: compose declares it.
[[ -e secrets/db_gateway_token.txt ]] || : > secrets/db_gateway_token.txt

chmod 600 secrets/*.txt
# The bot runs as uid 1000 and compose mounts secret files with host ownership.
if [[ "$(id -u)" -eq 0 ]]; then
  chown 1000:1000 secrets/*.txt
fi
echo "✅ Secrets ready in ./secrets (mode 600). Never commit this directory."
