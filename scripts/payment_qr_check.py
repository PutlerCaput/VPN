"""Does YooKassa give this shop SBP payments by QR? Run inside the bot container.

`update_bot.sh --payment-qr-check` pipes this file into the live container, and
`update_bot.sh --set YOOKASSA_CONFIRMATION=qr` runs it first and switches only
when it passes. It creates one test payment of 10 RUB with the QR confirmation
and looks at the answer: an SBP link (https://qr.nspk.ru/...) means the bot can
show the QR code itself, and the payer no longer needs the YooMoney checkout page
or its CDN.

Nobody can pay the test payment: its link is not shown or stored anywhere, and
YooKassa cancels it within an hour. Prints no secret and no link: the answer's
status, confirmation type and link host only.
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any
from urllib.parse import urlsplit

sys.path.insert(0, "/app")

CHECK_AMOUNT_RUB = 10


def link_host(link: Any) -> str | None:
    if not isinstance(link, str):
        return None
    try:
        return urlsplit(link).hostname
    except ValueError:
        return None


async def run_check(
    settings: Any, transport: Any = None
) -> tuple[bool, str, dict[str, Any]]:
    """(passed, one-line verdict, facts to print). Never raises for a refusal."""
    try:
        from services.yookassa import (
            YooKassaClient,
            YooKassaError,
            confirmation_link,
            is_sbp_link,
        )
    except ImportError:
        return False, "the running bot is older than this check: update it first", {}
    secret = getattr(settings, "yookassa_secret_key", None)
    if getattr(settings, "sbp_provider", None) != "yookassa" or not (
        getattr(settings, "yookassa_shop_id", None) and secret
    ):
        return False, "YooKassa REST keys are not set (SBP_PROVIDER=yookassa)", {}
    if settings.yookassa_receipt_mode != "self_employed":
        return False, "fiscal receipts need a buyer email: check with a real order", {}
    client = YooKassaClient(
        settings.yookassa_shop_id,
        secret.get_secret_value(),
        return_url=settings.yookassa_return,
        receipt_mode=settings.yookassa_receipt_mode,
        vat_code=settings.yookassa_vat_code,
        tax_system_code=settings.yookassa_tax_system_code,
        proxy_url=settings.yookassa_proxy_url,
        transport=transport,
        payment_method="sbp",
        confirmation="qr",
    )
    # A fresh key: the check never replays an earlier test payment.
    key = f"qrcheck-{uuid.uuid4().hex}"
    body = client.build_payment(
        payment_id=key, amount_rub=CHECK_AMOUNT_RUB, months=1, email=None
    )
    try:
        data = await client.submit_payment_request(key, body)
    except YooKassaError as exc:
        detail = exc.provider_code or exc.failure or "no details"
        return (
            False,
            f"YooKassa did not create a QR payment: HTTP {exc.status_code or '-'} "
            f"{detail}",
            {},
        )
    finally:
        await client.close()
    confirmation = data.get("confirmation")
    kind = confirmation.get("type") if isinstance(confirmation, dict) else None
    link = confirmation_link(confirmation)
    facts = {
        "test_payment_status": data.get("status"),
        "confirmation_type": kind,
        "link_host": link_host(link),
        "test_shop": data.get("test"),
    }
    if kind == "qr" and isinstance(link, str) and is_sbp_link(link):
        return (
            True,
            "YooKassa returns an SBP link; the bot can show the QR code itself",
            facts,
        )
    return False, "YooKassa answered without an SBP link", facts


def main() -> int:
    from config import get_settings

    try:
        passed, verdict, facts = asyncio.run(run_check(get_settings()))
    except Exception as exc:
        # The type only: an error text may carry a URL or a setting.
        passed, verdict, facts = False, f"the check failed: {type(exc).__name__}", {}
    if facts:
        print(json.dumps(facts, ensure_ascii=False, indent=1))
        print(
            f"A test payment of {CHECK_AMOUNT_RUB} RUB was created for this check; "
            "nobody can pay it, YooKassa cancels it within an hour.",
            flush=True,
        )
    print(f"PAYMENT_QR_CHECK_{'OK' if passed else 'FAILED'}: {verdict}", flush=True)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
