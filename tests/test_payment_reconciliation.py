"""Missed callbacks must not lose paid periods or credit an order twice."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

import services.payment_reconciliation as reconciliation
from services.cryptopay import CryptoPayClient
from services.payment_reconciliation import PaymentReconciler
from services.yookassa import YooKassaClient
from webhook_server import create_webhook_app

YID = "2f3a9c1e-000f-5000-8000-1a2b3c4d5e6f"
USER = 970001
DAY = 86400


async def order(services: Any, provider: str = "yookassa") -> dict[str, Any]:
    await services.db.create_user(USER, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(USER)
    await services.db.create_payment(
        payment_id="order-one",
        telegram_id=USER,
        amount=30000 if provider == "yookassa" else 350,
        currency="RUB" if provider == "yookassa" else "USDT",
        months=1,
        method="sbp" if provider == "yookassa" else "usdt",
        provider_id=f"{provider}:{YID if provider == 'yookassa' else '42'}",
    )
    return dict(await services.db.get_payment("order-one"))


def remote_yookassa(**updates: Any) -> dict[str, Any]:
    return {
        "id": YID,
        "status": "succeeded",
        "paid": True,
        "amount": {"value": "300.00", "currency": "RUB"},
        "metadata": {"payment_id": "order-one"},
        **updates,
    }


def remote_crypto(**updates: Any) -> dict[str, Any]:
    return {
        "invoice_id": 42,
        "status": "paid",
        "asset": "USDT",
        "amount": "3.50",
        "payload": json.dumps(
            {"telegram_id": USER, "months": 1, "payment_id": "order-one"}
        ),
        **updates,
    }


def yookassa_client(body: dict[str, Any], status: int = 200) -> YooKassaClient:
    return YooKassaClient(
        "123456",
        "test_" + "x" * 40,
        return_url="https://t.me/test_bot",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(status, json=body)
        ),
    )


def crypto_client(body: dict[str, Any]) -> CryptoPayClient:
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/getInvoices"
        assert json.loads(request.content) == {"invoice_ids": "42"}
        return httpx.Response(200, json={"ok": True, "result": {"items": [body]}})

    return CryptoPayClient("12345:test", transport=httpx.MockTransport(handle))


async def test_poll_recovers_missing_callback_once(services: Any) -> None:
    await order(services)
    before = (await services.db.get_user(USER))["expires_at"]
    client = yookassa_client(remote_yookassa())
    try:
        reconciler = PaymentReconciler(services.db, services.provisioning, client, None)
        await reconciler.poll()
        await reconciler.poll()
        assert (await services.db.get_payment("order-one"))["status"] == "paid"
        assert (await services.db.get_user(USER))["expires_at"] == before + 30 * DAY
        assert (await services.db.get_payment_activation("order-one"))[
            "status"
        ] == "completed"
    finally:
        await client.close()


async def test_concurrent_webhook_and_poll_add_exactly_one_period(
    services: Any,
) -> None:
    await order(services)
    before = (await services.db.get_user(USER))["expires_at"]
    client = yookassa_client(remote_yookassa())
    services.yookassa = client
    reconciler = PaymentReconciler(services.db, services.provisioning, client, None)
    app = create_webhook_app(services.bot, None, services)  # type: ignore[arg-type]
    try:
        async with TestClient(TestServer(app)) as http:
            responses = await asyncio.gather(
                http.post(
                    "/yookassa/webhook",
                    json={"event": "payment.succeeded", "object": {"id": YID}},
                ),
                reconciler.poll(),
                http.post(
                    "/yookassa/webhook",
                    json={"event": "payment.succeeded", "object": {"id": YID}},
                ),
            )
            assert responses[0].status == 200
            assert responses[2].status == 200
        assert (await services.db.get_user(USER))["expires_at"] == before + 30 * DAY
        assert (await services.db.get_payment("order-one"))["status"] == "paid"
    finally:
        await client.close()


async def test_outage_preserves_old_invoice_and_schedules_retry(services: Any) -> None:
    await order(services)
    now = int(time.time())
    await services.db._execute(
        "UPDATE payments SET created_at = ? WHERE payment_id = ?",
        (now - 7 * DAY, "order-one"),
    )
    client = yookassa_client({"type": "error"}, status=503)
    try:
        reconciler = PaymentReconciler(services.db, services.provisioning, client, None)
        await reconciler.poll()
        assert await services.db.cancel_stale_payments(now - 2 * DAY) == 0
        assert (await services.db.get_payment("order-one"))["status"] == "pending"
        assert not await services.db.payments_to_check(now)
        assert len(await services.db.payments_to_check(now + 180)) == 1
    finally:
        await client.close()


async def test_provider_cancellation_is_authoritative(services: Any) -> None:
    await order(services)
    client = yookassa_client(remote_yookassa(status="canceled", paid=False))
    try:
        await PaymentReconciler(services.db, services.provisioning, client, None).poll()
        assert (await services.db.get_payment("order-one"))["status"] == "cancelled"
        assert await services.db.get_payment_activation("order-one") is None
    finally:
        await client.close()


@pytest.mark.parametrize(
    "updates",
    [
        {"status": "pending", "paid": False},
        {"id": "00000000-0000-4000-8000-000000000000"},
        {"metadata": {"payment_id": "different-order"}},
        {"amount": {"value": "1.00", "currency": "RUB"}},
        {"amount": {"value": "300.00", "currency": "USD"}},
        {"amount": {"value": "NaN", "currency": "RUB"}},
        {"amount": {"value": "Infinity", "currency": "RUB"}},
        {"amount": {"value": "sNaN", "currency": "RUB"}},
        {"amount": {"value": "1e999999999", "currency": "RUB"}},
    ],
)
async def test_poll_rejects_unpaid_or_mismatched_yookassa_orders(
    services: Any, updates: dict[str, Any]
) -> None:
    await order(services)
    before = (await services.db.get_user(USER))["expires_at"]
    client = yookassa_client(remote_yookassa(**updates))
    try:
        await PaymentReconciler(services.db, services.provisioning, client, None).poll()
        assert (await services.db.get_payment("order-one"))["status"] == "pending"
        assert (await services.db.get_user(USER))["expires_at"] == before
    finally:
        await client.close()


async def test_crypto_poll_recovers_paid_invoice_and_expiry(services: Any) -> None:
    await order(services, "cryptopay")
    before = (await services.db.get_user(USER))["expires_at"]
    client = crypto_client(remote_crypto())
    try:
        poller = PaymentReconciler(services.db, services.provisioning, None, client)
        await poller.poll()
        await poller.poll()
        assert (await services.db.get_payment("order-one"))["status"] == "paid"
        assert (await services.db.get_user(USER))["expires_at"] == before + 30 * DAY
    finally:
        await client.close()


async def test_crypto_expired_invoice_is_cancelled(services: Any) -> None:
    await order(services, "cryptopay")
    client = crypto_client(remote_crypto(status="expired"))
    try:
        await PaymentReconciler(services.db, services.provisioning, None, client).poll()
        assert (await services.db.get_payment("order-one"))["status"] == "cancelled"
    finally:
        await client.close()


@pytest.mark.parametrize(
    "updates",
    [
        {
            "payload": json.dumps(
                {"telegram_id": USER + 1, "months": 1, "payment_id": "order-one"}
            )
        },
        {"amount": "0.01"},
        {"amount": "NaN"},
        {"amount": "Infinity"},
        {"amount": "sNaN"},
        {"amount": "1e999999999"},
        {"asset": "BTC"},
        {"status": "active"},
        {"payload": "[]"},
    ],
)
async def test_crypto_unpaid_or_mismatched_never_activates(
    services: Any, updates: dict[str, Any]
) -> None:
    await order(services, "cryptopay")
    before = (await services.db.get_user(USER))["expires_at"]
    client = crypto_client(remote_crypto(**updates))
    try:
        await PaymentReconciler(services.db, services.provisioning, None, client).poll()
        assert (await services.db.get_payment("order-one"))["status"] == "pending"
        assert (await services.db.get_user(USER))["expires_at"] == before
    finally:
        await client.close()


async def test_failing_oldest_order_does_not_starve_next_batch(
    services: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await order(services)
    await services.db.create_payment(
        payment_id="order-two",
        telegram_id=USER,
        amount=30000,
        currency="RUB",
        months=1,
        method="sbp",
        provider_id="yookassa:00000000-0000-4000-8000-000000000000",
    )
    monkeypatch.setattr(reconciliation, "CHECK_LIMIT", 1)
    client = yookassa_client({"type": "error"}, status=503)
    try:
        poller = PaymentReconciler(services.db, services.provisioning, client, None)
        await poller.poll()
        due = await services.db.payments_to_check(int(time.time()))
        assert [p["payment_id"] for p in due] == ["order-two"]
        await poller.poll()
        assert not await services.db.payments_to_check(int(time.time()))
    finally:
        await client.close()


async def test_timeout_keeps_order_and_retry_state(
    services: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    await order(services)
    client = yookassa_client(remote_yookassa())

    async def stalled(_: str) -> dict[str, Any]:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(client, "get_payment", AsyncMock(side_effect=stalled))
    monkeypatch.setattr(reconciliation, "CHECK_BUDGET_SECONDS", 0.02)
    try:
        await PaymentReconciler(services.db, services.provisioning, client, None).poll()
        assert (await services.db.get_payment("order-one"))["status"] == "pending"
        assert not await services.db.payments_to_check(int(time.time()))
    finally:
        await client.close()
