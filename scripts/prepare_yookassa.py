#!/usr/bin/env python3
"""Verify YooKassa credentials with GET /me; never change production configuration.

Docker is invoked only with fixed, read-only argument arrays and no shell.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import re
import subprocess  # nosec B404
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import warnings
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

API = "https://api.yookassa.ru/v3/me"
SETTINGS_CODE = """
import json
import hashlib
from pathlib import Path
from config import get_settings
from keyboards.main_menu import BTN_PAY, channel_kb
s = get_settings()
print(json.dumps({
    'shop_id': s.yookassa_shop_id,
    'secret_key': (s.yookassa_secret_key.get_secret_value()
                   if s.yookassa_secret_key else None),
    'public_base_url': s.public_base_url,
    'receipt_mode': s.yookassa_receipt_mode,
    'provider': s.sbp_provider,
    'payment_button': BTN_PAY,
    'payment_channels': [
        {'text': button.text, 'callback_data': button.callback_data}
        for row in channel_kb().inline_keyboard for button in row
    ],
    'source_sha256': {
        name: hashlib.sha256(Path(name).read_bytes()).hexdigest()
        for name in ('handlers/payments.py', 'keyboards/main_menu.py',
                     'services/billing.py', 'services/yookassa.py',
                     'main.py', 'webhook_server.py', 'legal/oferta.md')
    },
}))
"""


class CheckFailed(RuntimeError):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise CheckFailed("REDIRECT_REJECTED")


def hidden_input(prompt: str) -> str:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        return getpass.getpass(prompt).strip()


def validate_account(data: Any, shop_id: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise CheckFailed("INVALID_RESPONSE")
    if str(data.get("account_id", "")) != shop_id:
        raise CheckFailed("STORE_ID_MISMATCH")
    if data.get("test") is not False:
        raise CheckFailed("LIVE_STORE_REQUIRED")
    if data.get("status") != "enabled":
        raise CheckFailed("STORE_NOT_ENABLED")
    if data.get("fiscalization_enabled") is not True:
        raise CheckFailed("FISCALIZATION_NOT_ENABLED")
    methods = data.get("payment_methods")
    known_methods: set[str] | None = (
        set(methods)
        if isinstance(methods, list)
        and all(isinstance(value, str) for value in methods)
        else None
    )
    return {
        "authenticated": True,
        "live": True,
        "store_enabled": True,
        "fiscalization_enabled": True,
        "bank_card_available": (
            "bank_card" in known_methods if known_methods is not None else None
        ),
        "sbp_available": "sbp" in known_methods if known_methods is not None else None,
        "verified_at_utc": datetime.now(UTC).isoformat(),
    }


def candidate_webhook(base_url: str) -> str | None:
    try:
        parsed = urllib.parse.urlsplit(base_url)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or not host
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or host in {"localhost", "example.com"}
            or host.endswith((".example.com", ".invalid"))
        ):
            return None
        _ = parsed.port  # Validate the port without printing URLs.
        return urllib.parse.urlunsplit(
            (
                "https",
                parsed.netloc,
                parsed.path.rstrip("/") + "/yookassa/webhook",
                "",
                "",
            )
        )
    except ValueError:
        return None


def write_private(directory: Path, name: str, content: str) -> None:
    descriptor = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        output.write(content)
        output.flush()
        os.fsync(output.fileno())


def verify_credentials(shop_id: str, secret: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9]{1,32}", shop_id):
        raise CheckFailed("INVALID_SHOP_ID_FORMAT")
    if not secret or len(secret) > 4096 or any(c in secret for c in "\r\n\x00"):
        raise CheckFailed("INVALID_KEY_FORMAT")
    credentials = base64.b64encode((shop_id + ":" + secret).encode()).decode()
    request = urllib.request.Request(  # noqa: S310  # fixed HTTPS API
        API,
        headers={
            "Authorization": "Basic " + credentials,
            "Accept": "application/json",
            "User-Agent": "NodeRental-ReadOnly-ShopCheck/1.0",
        },
    )
    with urllib.request.build_opener(NoRedirect()).open(
        request, timeout=20
    ) as response:
        if response.status != 200:
            raise CheckFailed("UNEXPECTED_HTTP_STATUS")
        body = response.read(1024 * 1024 + 1)
    if len(body) > 1024 * 1024:
        raise CheckFailed("RESPONSE_TOO_LARGE")
    return validate_account(json.loads(body), shop_id)


def run(output_root: Path = Path("/root")) -> None:
    if os.geteuid() != 0:
        raise CheckFailed("RUN_ON_SERVER_AS_ROOT")
    result = subprocess.run(  # noqa: S603  # nosec
        # Docker is resolved from the administrator's PATH, no shell is used.
        ["docker", "exec", "node-rental-bot-1", "python", "-c", SETTINGS_CODE],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise CheckFailed("CANNOT_READ_EXISTING_BINDINGS")
    existing = json.loads(result.stdout)
    shop_id = str(existing.get("shop_id") or "").strip()
    secret = str(existing.get("secret_key") or "").strip()
    print("Значения вводятся скрыто; в чат их не отправляйте.", flush=True)
    if not shop_id:
        shop_id = hidden_input("shopId: ")
    else:
        print("Используется уже настроенный shopId.", flush=True)
    if not secret:
        secret = hidden_input("Секретный ключ ЮKassa: ")
    else:
        print("Используется уже настроенный ключ ЮKassa.", flush=True)
    summary = verify_credentials(shop_id, secret)
    summary["webhook_url_candidate"] = candidate_webhook(
        str(existing.get("public_base_url") or "")
    )
    summary["current_receipt_mode"] = existing.get("receipt_mode")
    for name in ("provider", "payment_button", "payment_channels", "source_sha256"):
        summary[name] = existing.get(name)
    directory = Path(tempfile.mkdtemp(prefix="yookassa-verified-", dir=output_root))
    directory.chmod(0o700)
    write_private(directory, "shop_id.txt", shop_id + "\n")
    write_private(directory, "secret_key.txt", secret + "\n")
    write_private(directory, "verification.json", json.dumps(summary, indent=2) + "\n")
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    print("YOOKASSA_CREDENTIALS_VERIFIED", flush=True)
    print("VERIFIED_SETTINGS_DIR=" + str(directory))
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("PRODUCTION_CONFIG_UNCHANGED: платежи не создавались, бот не перезапускался.")


if __name__ == "__main__":
    try:
        run()
    except urllib.error.HTTPError as error:
        print("YOOKASSA_CHECK_FAILED: HTTP_" + str(error.code))
        sys.exit(1)
    except CheckFailed as error:
        print("YOOKASSA_CHECK_FAILED: " + str(error))
        sys.exit(1)
    except Exception as error:
        print("YOOKASSA_CHECK_FAILED: " + type(error).__name__)
        sys.exit(1)
