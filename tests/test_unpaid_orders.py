"""Unpaid orders: two open at most, cancelled after an hour, late payments count."""

from __future__ import annotations

import time
from typing import Any

import pytest

from services.billing import BillingError
from services.payment_reconciliation import PaymentReconciler
from services.telegram_rub import TelegramRubPayments, payload_for
from tests.test_payment_reconciliation import (
    USER,
    order,
    remote_yookassa,
    yookassa_client,
)

HOUR = 3600
LIVE = "390540012:LIVE:12345"


async def _buyer(services: Any) -> None:
    await services.db.create_user(USER, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(USER)


async def _age(services: Any, payment_id: str, seconds: int) -> None:
    await services.db._execute(
        "UPDATE payments SET created_at = ? WHERE payment_id = ?",
        (int(time.time()) - seconds, payment_id),
    )


async def _status(services: Any, payment_id: str) -> str:
    row = await services.db.get_payment(payment_id)
    assert row is not None
    return str(row["status"])


async def test_at_most_two_open_orders(services: Any) -> None:
    await _buyer(services)
    await services.billing.create_telegram_rub(USER, 1, "standard")
    await services.billing.create_telegram_rub(USER, 2, "standard")
    with pytest.raises(BillingError, match="too_many_pending"):
        await services.billing.create_telegram_rub(USER, 3, "standard")


async def test_unpaid_order_is_cancelled_after_an_hour(services: Any) -> None:
    await _buyer(services)
    old = await services.billing.create_telegram_rub(USER, 1, "standard")
    fresh = await services.billing.create_telegram_rub(USER, 2, "standard")
    await _age(services, old.payment_id, 2 * HOUR)
    cancelled = await services.db.cancel_stale_payments(int(time.time()) - HOUR)
    assert cancelled == 1
    assert await _status(services, old.payment_id) == "cancelled"
    assert await _status(services, fresh.payment_id) == "pending"


async def test_late_telegram_payment_of_cancelled_order_is_credited(
    services: Any,
) -> None:
    await _buyer(services)
    created = await services.billing.create_telegram_rub(USER, 1, "standard")
    await _age(services, created.payment_id, 2 * HOUR)
    await services.db.cancel_stale_payments(int(time.time()) - HOUR)
    rub = TelegramRubPayments(
        services.db, services.provisioning, services.settings.admin_telegram_id, LIVE
    )
    assert await rub.record_success(
        payload=payload_for(created.payment_id),
        from_id=USER,
        currency="RUB",
        total_amount=created.data["amount_kopecks"],
        provider_charge_id="late-tg-1",
    )
    assert await _status(services, created.payment_id) == "paid"


async def test_provider_bound_order_cancelled_only_when_provider_says_unpaid(
    services: Any,
) -> None:
    await _buyer(services)
    await order(services)
    await _age(services, "order-one", 2 * HOUR)
    # Provider did not answer: the order must stay open.
    outage = PaymentReconciler(
        services.db,
        services.provisioning,
        yookassa_client({"type": "error"}, 503),
        None,
    )
    await outage.poll()
    assert await _status(services, "order-one") == "pending"
    await services.db._execute("DELETE FROM payment_checks")
    # Provider answers "not paid yet": cancel.
    unpaid = PaymentReconciler(
        services.db,
        services.provisioning,
        yookassa_client(remote_yookassa(status="pending", paid=False)),
        None,
    )
    await unpaid.poll()
    assert await _status(services, "order-one") == "cancelled"


async def test_late_yookassa_payment_after_cancellation_is_credited(
    services: Any,
) -> None:
    await _buyer(services)
    await order(services)
    await _age(services, "order-one", 2 * HOUR)
    await services.db._execute("DELETE FROM payment_checks")
    await PaymentReconciler(
        services.db,
        services.provisioning,
        yookassa_client(remote_yookassa(status="pending", paid=False)),
        None,
    ).poll()
    assert await _status(services, "order-one") == "cancelled"
    # The customer paid after all: the provider confirms it and the period counts.
    await services.db._execute("DELETE FROM payment_checks")
    await PaymentReconciler(
        services.db, services.provisioning, yookassa_client(remote_yookassa()), None
    ).poll()
    assert await _status(services, "order-one") == "paid"


async def test_cancelled_order_outside_late_window_is_not_polled(services: Any) -> None:
    await _buyer(services)
    await order(services)
    await services.db._execute(
        "UPDATE payments SET status = 'cancelled', created_at = ? WHERE payment_id = ?",
        (int(time.time()) - 2 * 24 * HOUR, "order-one"),
    )
    assert not await services.db.payments_to_check(int(time.time()))
