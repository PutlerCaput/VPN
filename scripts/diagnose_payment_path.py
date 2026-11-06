#!/usr/bin/env python3
"""Read-only RU/DE diagnostics. Never prints service command lines or secrets."""

from __future__ import annotations

import argparse
import json
import re
import socket
import subprocess  # nosec B404
import urllib.error
import urllib.request
from typing import Any

SAFE_STATE = re.compile(r"[A-Za-z0-9_=\n./ -]{0,2048}\Z")
BOT_CONFIG = """
import asyncio
import json
import httpx
from config import get_settings

async def check():
    s = get_settings()
    shop = getattr(s, 'yookassa_shop_id', None)
    secret = getattr(s, 'yookassa_secret_key', None)
    report = {
        'provider': s.sbp_provider,
        'shop_id_set': bool(shop),
        'yookassa_secret_set': bool(secret),
        'database_encrypted': bool(s.db_key),
        'activity_logging': getattr(s, 'activity_logging_enabled', 'unsupported'),
        'payment_reconciliation': __import__('importlib.util').util.find_spec(
            'services.payment_reconciliation') is not None,
        'configured_egress': getattr(s, 'yookassa_egress', 'unsupported'),
        'configured_ru_proxy': getattr(s, 'yookassa_proxy_url', None)
            == 'http://10.203.0.1:3128',
    }
    if not shop or not secret:
        report['account_check'] = 'REST_CREDENTIALS_MISSING'
        print(json.dumps(report))
        return
    # This diagnostic follows the owner's RU-only payment route even if an
    # older running bot has no routing support. It never changes that bot.
    report['diagnostic_account_route'] = 'RU gateway (diagnostic only)'
    try:
        async with httpx.AsyncClient(
            auth=(str(shop), secret.get_secret_value()),
            proxy='http://10.203.0.1:3128',
            timeout=8,
            follow_redirects=False,
        ) as client:
            async with client.stream(
                'GET', 'https://api.yookassa.ru/v3/me'
            ) as response:
                report['account_http'] = response.status_code
                if response.status_code != 200:
                    print(json.dumps(report))
                    return
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 1048576:
                        raise ValueError('response too large')
                data = json.loads(body)
        if not isinstance(data, dict):
            raise ValueError('unexpected account response')
        report['account_matches_configured_shop'] = (
            str(data.get('account_id', '')) == str(shop))
        report['test_store'] = (
            data.get('test') if type(data.get('test')) is bool else None)
        report['store_enabled'] = data.get('status') == 'enabled'
        methods = data.get('payment_methods')
        report['bank_card_available'] = (
            'bank_card' in methods if isinstance(methods, list) else None)
        report['sbp_available'] = (
            'sbp' in methods if isinstance(methods, list) else None)
        # This field alone does not establish the seller's My Tax integration.
        report['fiscalization_enabled'] = (
            data.get('fiscalization_enabled')
            if type(data.get('fiscalization_enabled')) is bool else None)
    except Exception as error:
        report['account_error_type'] = type(error).__name__
    print(json.dumps(report))

try:
    asyncio.run(check())
except Exception as error:
    print(json.dumps({'diagnostic_error_type': type(error).__name__}))
"""


def command(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(  # noqa: S603  # nosec B603
            args, capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def unit_status(unit: str) -> str:
    result = command(
        [
            "systemctl",
            "show",
            unit,
            "--no-pager",
            "-p",
            "LoadState",
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "Result",
        ]
    )
    if result is None or result.returncode:
        return "unavailable"
    value = result.stdout.strip()
    return value if SAFE_STATE.fullmatch(value) else "unrecognized"


def probe(url: str, *, private: bool = False, webhook: bool = False) -> int | str:
    # The fixed private endpoints must be tested on the owner server, directly.
    # Public requests retain the host's configured proxy and CA trust.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}) if private else urllib.request.ProxyHandler()
    )
    request = urllib.request.Request(  # noqa: S310  # nosec B310
        url,
        data=b"{}" if webhook else None,
        headers={"Content-Type": "application/json"} if webhook else {},
    )
    try:
        with opener.open(request, timeout=8) as response:
            return int(response.status)
    except urllib.error.HTTPError as error:
        return error.code
    except (OSError, urllib.error.URLError) as error:
        return type(error).__name__


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=("ru", "de"))
    args = parser.parse_args()
    report: dict[str, Any] = {"role": args.role, "hostname": socket.gethostname()}
    report["wireguard_unit"] = unit_status("wg-quick@wg-payments.service")
    if args.role == "ru":
        report["cloudflared_unit"] = unit_status("cloudflared.service")
        report["payment_proxy_unit"] = unit_status("node-payment-proxy.service")
        report["ru_origin_root"] = probe("http://127.0.0.1:18080/", private=True)
        report["ru_to_de_webhook"] = probe(
            "http://10.203.0.2:18081/yookassa/webhook", private=True, webhook=True
        )
    else:
        report["payment_backend_unit"] = unit_status("node-payment-backend.service")
        report["bot_health"] = probe("http://127.0.0.1:8080/healthz", private=True)
        report["bot_webhook"] = probe(
            "http://127.0.0.1:8080/yookassa/webhook", private=True, webhook=True
        )
        result = command(
            ["docker", "exec", "node-rental-bot-1", "python", "-c", BOT_CONFIG]
        )
        if result is not None and not result.returncode:
            try:
                report["bot_config"] = json.loads(result.stdout)
            except ValueError:
                report["bot_config"] = "unrecognized"
        else:
            report["bot_config"] = "unavailable"
    report["public_webhook"] = probe(
        "https://pay.cyberdefency.com/yookassa/webhook", webhook=True
    )
    # Empty notifications cannot create or credit a payment. HTTP200 here
    # proves only route/handler access; a real captured invoice is still needed.
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
