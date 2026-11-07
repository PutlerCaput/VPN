"""A double tap on a pay button must not issue two invoices."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from services.billing import BillingService

USER = 993001


async def _billing(services: Any) -> BillingService:
    await services.db.create_user(USER, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(USER)
    return BillingService(
        services.db, services.sbp, None, stars_rub_per_star=Decimal("1.60")
    )


async def test_concurrent_stars_orders_share_one_invoice(services: Any) -> None:
    billing = await _billing(services)

    first, second = await asyncio.gather(
        billing.create(USER, 1, "stars", "standard"),
        billing.create(USER, 1, "stars", "standard"),
    )

    assert first.payment_id == second.payment_id
    assert await services.db.count_pending_payments(USER) == 1


async def test_concurrent_autorenew_requests_share_one_subscription(
    services: Any,
) -> None:
    billing = await _billing(services)

    first, second = await asyncio.gather(
        billing.create_stars_subscription(USER, "standard"),
        billing.create_stars_subscription(USER, "standard"),
    )

    assert first.payment_id == second.payment_id
