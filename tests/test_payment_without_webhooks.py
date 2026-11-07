"""Payments are credited without provider notifications (webhooks)."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock

import pytest

import handlers.payments as payments_handlers
from keyboards.main_menu import yookassa_payment_kb
from services.payment_reconciliation import (
    FRESH_RETRY_SECONDS,
    LATE_RETRY_SECONDS,
    OPEN_RETRY_SECONDS,
    PaymentReconciler,
    retry_delay,
)
from services.security import SlidingWindowLimiter
from tests.test_payment_reconciliation import (
    USER,
    order,
    remote_yookassa,
    yookassa_client,
)


def test_fresh_orders_are_checked_often_and_old_ones_rarely() -> None:
    now = int(time.time())
    assert retry_delay({"status": "pending", "created_at": now - 60}, now) == (
        FRESH_RETRY_SECONDS
    )
    assert retry_delay({"status": "pending", "created_at": now - 3600}, now) == (
        OPEN_RETRY_SECONDS
    )
    assert retry_delay({"status": "cancelled", "created_at": now - 60}, now) == (
        LATE_RETRY_SECONDS
    )
    assert FRESH_RETRY_SECONDS <= 15


async def test_poll_schedules_a_fresh_order_within_seconds(services: Any) -> None:
    await order(services)
    client = yookassa_client(remote_yookassa(status="pending", paid=False))
    try:
        await PaymentReconciler(services.db, services.provisioning, client, None).poll()
    finally:
        await client.close()
    row = await services.db._fetchone(
        "SELECT next_check_at FROM payment_checks WHERE payment_id = 'order-one'", ()
    )
    assert row["next_check_at"] - int(time.time()) <= FRESH_RETRY_SECONDS


async def test_check_now_credits_a_paid_order_once(services: Any) -> None:
    await order(services)
    before = (await services.db.get_user(USER))["expires_at"]
    client = yookassa_client(remote_yookassa())
    reconciler = PaymentReconciler(services.db, services.provisioning, client, None)
    try:
        assert await reconciler.check_now(USER, "order-one") == "paid"
        assert await reconciler.check_now(USER, "order-one") == "paid"
    finally:
        await client.close()
    assert (await services.db.get_user(USER))["expires_at"] == before + 30 * 86400


async def test_check_now_is_owner_only_and_reports_pending_and_outage(
    services: Any,
) -> None:
    await order(services)
    pending = yookassa_client(remote_yookassa(status="pending", paid=False))
    outage = yookassa_client({"type": "error"}, 503)
    try:
        reconciler = PaymentReconciler(
            services.db, services.provisioning, pending, None
        )
        assert await reconciler.check_now(USER + 1, "order-one") == "not_found"
        assert await reconciler.check_now(USER, "missing") == "not_found"
        assert await reconciler.check_now(USER, "order-one") == "pending"
        down = PaymentReconciler(services.db, services.provisioning, outage, None)
        assert await down.check_now(USER, "order-one") == "unavailable"
    finally:
        await pending.close()
        await outage.close()
    assert (await services.db.get_payment("order-one"))["status"] == "pending"


def test_payment_message_has_a_check_button() -> None:
    rows = yookassa_payment_kb(
        "https://yoomoney.ru/checkout/x", "a" * 32
    ).inline_keyboard
    assert rows[0][0].url == "https://yoomoney.ru/checkout/x"
    assert rows[1][0].callback_data == "paycheck:" + "a" * 32
    assert len(rows[1][0].callback_data.encode()) <= 64


@pytest.mark.parametrize(
    ("remote", "status", "alert"),
    [
        (remote_yookassa(), "paid", False),
        (remote_yookassa(status="pending", paid=False), "pending", True),
    ],
)
async def test_check_button_answers_and_is_rate_limited(
    services: Any, monkeypatch: Any, remote: dict[str, Any], status: str, alert: bool
) -> None:
    await order(services)
    client = yookassa_client(remote)
    services.billing.yookassa = client
    monkeypatch.setattr(
        payments_handlers, "PAYCHECK_LIMIT", SlidingWindowLimiter(1, 60)
    )
    callback = AsyncMock()
    callback.data = "paycheck:order-one"
    callback.from_user.id = USER
    try:
        # "order-one" is not a 32-hex id: the handler ignores it silently.
        await payments_handlers.cb_check_payment(callback, services)
        assert callback.answer.await_args.args == ()
        valid = "b" * 32
        await services.db._execute(
            "UPDATE payments SET payment_id = ? WHERE payment_id = 'order-one'",
            (valid,),
        )
        client_remote = dict(remote, metadata={"payment_id": valid})
        await client.close()
        client = yookassa_client(client_remote)
        services.billing.yookassa = client
        callback.data = f"paycheck:{valid}"
        await payments_handlers.cb_check_payment(callback, services)
        text = callback.answer.await_args.args[0]
        assert text == payments_handlers.PAYCHECK_ANSWERS[status]
        assert callback.answer.await_args.kwargs["show_alert"] is alert
        await payments_handlers.cb_check_payment(callback, services)
        assert "Слишком часто" in callback.answer.await_args.args[0]
    finally:
        await client.close()
