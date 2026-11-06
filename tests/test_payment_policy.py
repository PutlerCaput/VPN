"""New public ruble invoices require YooKassa; separate USDT remains usable."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from services.billing import BillingError, BillingService


async def test_no_silent_manual_fallback_without_yookassa(services: Any) -> None:
    await services.db.create_user(891001, "customer", None)
    await services.db.accept_offer(891001)
    billing = BillingService(services.db, services.sbp, services.cryptopay)
    with pytest.raises(BillingError, match="ruble_payments_unavailable"):
        await billing.create(891001, 1, "sbp")
    assert await services.db.count_pending_payments(891001) == 0


async def test_separate_usdt_invoice_works_with_legacy_rubles_disabled(
    services: Any,
) -> None:
    await services.db.create_user(891002, "customer", None)
    await services.db.accept_offer(891002)
    provider = AsyncMock()
    provider.create_invoice.return_value = {
        "invoice_id": 883311,
        "bot_invoice_url": "https://t.me/CryptoBot?start=IV883311",
    }
    billing = BillingService(services.db, services.sbp, provider)
    payment = await billing.create(891002, 3, "usdt", "prime")
    stored = await services.db.get_payment(payment.payment_id)
    assert stored["currency"] == "USDT"
    assert stored["provider_id"] == "cryptopay:883311"
    assert stored["status"] == "pending"
    assert stored["amount"] == 2050
    assert payment.data["pay_url"] == "https://t.me/CryptoBot?start=IV883311"
    provider.create_invoice.assert_awaited_once()
