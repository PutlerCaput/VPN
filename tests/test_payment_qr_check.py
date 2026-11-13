"""update_bot.sh --payment-qr-check: does YooKassa give this shop SBP by QR?

The bot is switched to the QR confirmation only after this check has seen a
real answer with an SBP link. It never prints a secret or the link itself.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from tests.test_payment_creation import SBP_LINK

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "payment_qr_check.py"
spec = importlib.util.spec_from_file_location("payment_qr_check", SCRIPT)
assert spec is not None
assert spec.loader is not None
check: Any = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)

SECRET = "live_" + "s" * 40


def settings(**values: Any) -> SimpleNamespace:
    base = {
        "sbp_provider": "yookassa",
        "yookassa_shop_id": "123456",
        "yookassa_secret_key": SecretStr(SECRET),
        "yookassa_receipt_mode": "self_employed",
        "yookassa_vat_code": 1,
        "yookassa_tax_system_code": 2,
        "yookassa_proxy_url": None,
        "yookassa_return": "https://t.me/test_bot",
    }
    return SimpleNamespace(**{**base, **values})


class Shop:
    """The YooKassa API as the check sees it."""

    def __init__(self, confirmation: Any = None, status: int = 200) -> None:
        self.confirmation = confirmation or {
            "type": "qr",
            "confirmation_data": SBP_LINK,
        }
        self.status = status
        self.requests: list[tuple[httpx.Request, dict[str, Any]]] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.requests.append((request, body))
        if self.status != 200:
            return httpx.Response(
                self.status,
                json={
                    "type": "error",
                    "code": "invalid_request",
                    "description": "Confirmation type is not supported",
                },
            )
        return httpx.Response(
            200,
            json={
                "id": "2c1b2a3d-000f-5000-8000-1a2b3c4d5e6f",
                "status": "pending",
                "paid": False,
                "amount": body["amount"],
                "metadata": body["metadata"],
                "confirmation": self.confirmation,
                "test": False,
            },
        )


async def run(shop: Shop, **values: Any) -> tuple[bool, str, dict[str, Any]]:
    result: tuple[bool, str, dict[str, Any]] = await check.run_check(
        settings(**values), httpx.MockTransport(shop.handle)
    )
    return result


async def test_an_sbp_link_passes_and_is_never_printed() -> None:
    shop = Shop()
    passed, verdict, facts = await run(shop)
    assert passed, verdict
    assert facts == {
        "test_payment_status": "pending",
        "confirmation_type": "qr",
        "link_host": "qr.nspk.ru",
        "test_shop": False,
    }
    ((request, body),) = shop.requests
    assert body["confirmation"] == {"type": "qr", "return_url": "https://t.me/test_bot"}
    assert body["payment_method_data"] == {"type": "sbp"}
    assert body["amount"] == {"value": "10.00", "currency": "RUB"}
    key = request.headers["Idempotence-Key"]
    assert key.startswith("qrcheck-")
    assert body["metadata"] == {"payment_id": key}
    assert SBP_LINK not in verdict + json.dumps(facts)


async def test_every_check_is_a_new_test_payment() -> None:
    shop = Shop()
    await run(shop)
    await run(shop)
    keys = {request.headers["Idempotence-Key"] for request, _ in shop.requests}
    assert len(keys) == 2


async def test_a_refusal_fails_with_the_code_only() -> None:
    passed, verdict, facts = await run(Shop(status=400))
    assert not passed
    assert verdict == ("YooKassa did not create a QR payment: HTTP 400 invalid_request")
    assert facts == {}
    assert SECRET not in verdict


@pytest.mark.parametrize(
    "confirmation",
    [
        {"type": "redirect", "confirmation_url": "https://yoomoney.ru/checkout/x"},
        {"type": "qr", "confirmation_data": "https://example.com/pay"},
        {"type": "qr", "confirmation_data": "sbp://qr.nspk.ru/AS1A"},
        {"type": "qr"},
    ],
)
async def test_anything_but_an_sbp_link_fails(confirmation: dict[str, Any]) -> None:
    passed, verdict, facts = await run(Shop(confirmation))
    assert not passed
    assert verdict == "YooKassa answered without an SBP link"
    assert facts["confirmation_type"] == confirmation["type"]


@pytest.mark.parametrize(
    "values",
    [
        {"sbp_provider": "manual"},
        {"yookassa_shop_id": None},
        {"yookassa_secret_key": None},
        {"yookassa_receipt_mode": "fiscal"},
    ],
)
async def test_nothing_is_sent_without_keys_or_with_fiscal_receipts(
    values: dict[str, Any],
) -> None:
    shop = Shop()
    passed, _, facts = await run(shop, **values)
    assert not passed
    assert facts == {}
    assert shop.requests == []


def test_the_check_works_piped_into_the_container() -> None:
    """update_bot.sh runs it as `python3 - < payment_qr_check.py`."""
    with SCRIPT.open() as script:
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-"],
            stdin=script,
            cwd=ROOT,
            env={**os.environ, "SBP_PROVIDER": "manual"},
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    assert result.returncode == 1
    assert result.stdout.strip() == (
        "PAYMENT_QR_CHECK_FAILED: "
        "YooKassa REST keys are not set (SBP_PROVIDER=yookassa)"
    )
    assert "Traceback" not in result.stderr
