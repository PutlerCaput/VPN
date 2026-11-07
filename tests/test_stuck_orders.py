"""Orders stuck before their link are voided; a customer can always order again."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

import handlers.payments as payments_handlers
from services.billing import BillingError
from services.yookassa import YooKassaClient
from tests.test_payment_creation import Provider

USER = 990011
PAGE = "https://yoomoney.ru/checkout/test"  # the fake provider's checkout link
DAY = 86400


@pytest.fixture
async def provider(services: Any) -> Any:
    fake = Provider()
    client = YooKassaClient(
        "123456",
        "test_key",
        return_url="https://app.test",
        transport=httpx.MockTransport(fake.handle),
    )
    services.yookassa = client
    services.billing.yookassa = client
    services.settings = services.settings.model_copy(
        update={"yookassa_shop_id": "123456"}
    )
    yield fake
    await client.close()


async def _buyer(services: Any) -> None:
    await services.db.create_user(USER, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(USER)


async def _order(
    services: Any,
    payment_id: str,
    *,
    shop: str = "123456",
    state: str = "pending",
    url: str | None = None,
    lease_until: int = 0,
    retry_until: int | None = None,
    months: int = 1,
    tier: str = "standard",
) -> None:
    """An order exactly as the creation service leaves it."""
    now = int(time.time())
    body = json.dumps(
        {
            "amount": {"value": f"{299 * months}.00", "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": "https://app.test"},
            "metadata": {"payment_id": payment_id},
            "description": "test",
        }
    )
    await services.db._execute(
        "INSERT INTO payments (payment_id, telegram_id, amount, currency, months, "
        "method, tier, created_at) VALUES (?, ?, ?, 'RUB', ?, 'sbp', ?, ?)",
        (payment_id, USER, 29900 * months, months, tier, now),
    )
    await services.db._execute(
        "INSERT INTO yookassa_creations (payment_id, shop_id, request_body, "
        "receipt_mode, state, confirmation_url, retry_until, lease_until) "
        "VALUES (?, ?, ?, 'self_employed', ?, ?, ?, ?)",
        (payment_id, shop, body, state, url, retry_until or now + DAY, lease_until),
    )


async def _status(services: Any, payment_id: str) -> str:
    row = await services.db.get_payment(payment_id)
    assert row is not None
    return str(row["status"])


async def test_sweep_voids_only_orders_without_a_link(services: Any) -> None:
    await _buyer(services)
    now = int(time.time())
    ids = {
        name: uuid.uuid4().hex
        for name in ("review", "expired", "foreign", "linked", "leased", "fresh")
    }
    await _order(services, ids["review"], state="review")
    await _order(services, ids["expired"], retry_until=now - 10)
    await _order(services, ids["foreign"], shop="999999")
    await _order(services, ids["linked"], state="ready", url=PAGE)
    await _order(services, ids["leased"], retry_until=now - 10, lease_until=now + 600)
    await _order(services, ids["fresh"])

    assert await services.db.void_stuck_orders(now, "123456") == 3
    assert await _status(services, ids["review"]) == "cancelled"
    assert await _status(services, ids["expired"]) == "cancelled"
    assert await _status(services, ids["foreign"]) == "cancelled"
    # A link already sent, a request in flight and a fresh order stay open.
    assert await _status(services, ids["linked"]) == "pending"
    assert await _status(services, ids["leased"]) == "pending"
    assert await _status(services, ids["fresh"]) == "pending"


async def test_stuck_order_does_not_block_a_new_one(
    services: Any, provider: Any
) -> None:
    await _buyer(services)
    stuck = uuid.uuid4().hex
    await _order(services, stuck, state="review", months=1)
    created = await services.billing.create(USER, 3, "sbp", "standard")
    assert created.data["payment_url"] == PAGE
    assert await _status(services, stuck) == "cancelled"
    assert len(provider.calls) == 1


async def test_link_of_another_shop_is_never_offered(
    services: Any, provider: Any
) -> None:
    await _buyer(services)
    old = uuid.uuid4().hex
    await _order(services, old, shop="999999", state="ready", url=PAGE)
    message = AsyncMock()
    await payments_handlers.show_open_payment(message, USER, services)
    assert "прежним магазином" in message.answer.await_args.args[0]
    rows = message.answer.await_args.kwargs["reply_markup"].inline_keyboard
    assert [b.callback_data for row in rows for b in row] == [f"paycancel:{old}"]

    with pytest.raises(BillingError) as blocked:
        await services.billing.create(USER, 2, "sbp", "standard")
    assert blocked.value.code == "too_many_pending"
    # The customer cancels the foreign order and can then order again.
    assert await services.db.cancel_own_payment(USER, old)
    created = await services.billing.create(USER, 2, "sbp", "standard")
    assert created.data["payment_url"] == PAGE


async def test_retry_button_completes_an_order_waiting_for_its_link(
    services: Any, provider: Any
) -> None:
    await _buyer(services)
    waiting = uuid.uuid4().hex
    await _order(services, waiting, state="pending")
    message = AsyncMock()
    await payments_handlers.show_open_payment(message, USER, services)
    rows = message.answer.await_args.kwargs["reply_markup"].inline_keyboard
    assert [b.callback_data for row in rows for b in row] == [f"paycreate:{waiting}"]

    callback = AsyncMock()
    callback.data = f"paycreate:{waiting}"
    callback.from_user.id = USER
    callback.message = None
    await payments_handlers.cb_retry_payment_creation(callback, services)
    assert callback.answer.await_args.args == ("Ссылка на оплату готова",)
    assert len(provider.calls) == 1
    creation = await services.db.get_yookassa_creation(waiting)
    assert creation is not None
    assert creation["state"] == "ready"
    assert creation["confirmation_url"] == PAGE


def test_voided_orders_are_logged_with_a_fixed_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from logging_setup import JsonFormatter

    with caplog.at_level(logging.WARNING):
        logging.getLogger("services.payment_creation").warning(
            "payment_creation_voided", extra={"reason": "retry_window"}
        )
    record = [r for r in caplog.records if r.getMessage() == "payment_creation_voided"][
        -1
    ]
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"] == "payment_creation_voided"
    assert payload["reason"] == "retry_window"
