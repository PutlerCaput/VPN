"""A refused YooKassa request voids its order; a temporary failure can be retried."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

import handlers.payments as payments_handlers
from services.billing import BillingError
from services.yookassa import YooKassaClient, YooKassaRejected

USER = 880011
PAGE = "https://yoomoney.ru/checkout/payments/v2/contract?orderId=test"
REFUSAL = {
    "type": "error",
    "code": "invalid_request",
    "description": "Payment method sbp is not available for this shop",
}


def client(handler: Any) -> YooKassaClient:
    return YooKassaClient(
        "123456",
        "test_secretkey",
        return_url="https://t.me/test_node_bot",
        transport=httpx.MockTransport(handler),
    )


def refusing(request: httpx.Request) -> httpx.Response:
    return httpx.Response(400, json=REFUSAL)


def accepting(calls: list[str], *, fail_first: bool = False) -> Any:
    def handle(request: httpx.Request) -> httpx.Response:
        key = request.headers["Idempotence-Key"]
        calls.append(key)
        if fail_first and len(calls) == 1:
            return httpx.Response(503, json={"type": "error", "code": "internal_error"})
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": str(uuid.UUID(key)),
                "status": "pending",
                "paid": False,
                "amount": body["amount"],
                "metadata": body["metadata"],
                "confirmation": {"type": "redirect", "confirmation_url": PAGE},
            },
        )

    return handle


async def _buyer(services: Any) -> None:
    await services.db.create_user(USER, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(USER)


async def _open_payment_id(services: Any) -> str:
    order = await services.db.open_payment(USER)
    assert order is not None
    return str(order["payment_id"])


def _callback(data: str) -> AsyncMock:
    callback = AsyncMock()
    callback.data = data
    callback.from_user.id = USER
    callback.message = None
    return callback


async def test_refused_order_is_void_and_never_retried(services: Any) -> None:
    await _buyer(services)
    provider = client(refusing)
    services.billing.yookassa = provider
    try:
        for _ in range(2):
            with pytest.raises(BillingError) as refused:
                await services.billing.create(USER, 1, "sbp", "standard")
            assert refused.value.code == "payment_method_unavailable"
    finally:
        await provider.close()
    # Nothing stays open, so the customer can choose another period at once.
    assert await services.db.count_pending_payments(USER) == 0
    later = int(time.time()) + 86400
    assert await services.db.yookassa_creations_to_retry(later) == []


async def test_refusal_names_yookassa_code_and_hides_the_secret() -> None:
    provider = client(refusing)
    try:
        with pytest.raises(YooKassaRejected) as refused:
            await provider.create_payment(
                payment_id=uuid.uuid4().hex,
                amount_rub=299,
                months=1,
                email=None,
                tier_title="Стандарт",
            )
    finally:
        await provider.close()
    text = str(refused.value)
    assert "HTTP 400" in text
    assert "invalid_request" in text
    assert "Payment method sbp is not available" in text
    assert "test_secretkey" not in text


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "payment_method_unavailable"),
        (403, "payment_method_unavailable"),
        (429, "provider_error"),
        (500, "provider_error"),
    ],
)
async def test_only_definitive_answers_void_the_order(
    services: Any, status: int, code: str
) -> None:
    await _buyer(services)

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"type": "error", "code": "x"})

    provider = client(answer)
    services.billing.yookassa = provider
    try:
        with pytest.raises(BillingError) as err:
            await services.billing.create(USER, 1, "sbp", "standard")
    finally:
        await provider.close()
    assert err.value.code == code


async def test_temporary_failure_keeps_the_order_and_retry_completes_it(
    services: Any,
) -> None:
    await _buyer(services)
    calls: list[str] = []
    provider = client(accepting(calls, fail_first=True))
    services.billing.yookassa = provider
    try:
        with pytest.raises(BillingError) as first:
            await services.billing.create(USER, 1, "sbp", "standard")
        assert first.value.code == "provider_error"
        payment_id = await _open_payment_id(services)

        message = AsyncMock()
        await payments_handlers.show_open_payment(message, USER, services)
        markup = message.answer.await_args.kwargs["reply_markup"]
        buttons = [b.callback_data for row in markup.inline_keyboard for b in row]
        assert buttons == [f"paycreate:{payment_id}"]

        callback = _callback(f"paycreate:{payment_id}")
        await payments_handlers.cb_retry_payment_creation(callback, services)
        assert callback.answer.await_args.args == ("Ссылка на оплату готова",)
    finally:
        await provider.close()
    creation = await services.db.get_yookassa_creation(payment_id)
    assert creation is not None
    assert creation["state"] == "ready"
    assert creation["confirmation_url"] == PAGE
    assert len(calls) == 2


async def test_retry_never_reopens_a_cancelled_order(services: Any) -> None:
    await _buyer(services)
    calls: list[str] = []
    provider = client(accepting(calls, fail_first=True))
    services.billing.yookassa = provider
    try:
        with pytest.raises(BillingError):
            await services.billing.create(USER, 1, "sbp", "standard")
        payment_id = await _open_payment_id(services)
        await services.db._execute(
            "UPDATE payments SET status = 'cancelled' WHERE payment_id = ?",
            (payment_id,),
        )
        callback = _callback(f"paycreate:{payment_id}")
        await payments_handlers.cb_retry_payment_creation(callback, services)
    finally:
        await provider.close()
    assert callback.answer.await_args.args == ("Счёт уже не активен.",)
    # No request reached YooKassa for the cancelled order.
    assert len(calls) == 1


async def test_reviewed_order_shows_support_and_no_buttons(services: Any) -> None:
    await _buyer(services)
    calls: list[str] = []
    provider = client(accepting(calls, fail_first=True))
    services.billing.yookassa = provider
    try:
        with pytest.raises(BillingError):
            await services.billing.create(USER, 1, "sbp", "standard")
        payment_id = await _open_payment_id(services)
        await services.db._execute(
            "UPDATE yookassa_creations SET state = 'review' WHERE payment_id = ?",
            (payment_id,),
        )
        message = AsyncMock()
        await payments_handlers.show_open_payment(message, USER, services)
    finally:
        await provider.close()
    assert message.answer.await_args.kwargs["reply_markup"] is None
    assert "проверяется" in message.answer.await_args.args[0]


async def test_log_line_carries_status_and_code_but_not_the_text(
    services: Any, caplog: pytest.LogCaptureFixture
) -> None:
    from logging_setup import JsonFormatter

    await _buyer(services)
    provider = client(refusing)
    services.billing.yookassa = provider
    caplog.set_level("ERROR")
    try:
        with pytest.raises(BillingError):
            await services.billing.create(USER, 1, "sbp", "standard")
    finally:
        await provider.close()
    records = [r for r in caplog.records if r.getMessage() == "yookassa_create_failed"]
    assert records
    line = JsonFormatter().format(records[-1])
    payload = json.loads(line)
    assert payload["event"] == "yookassa_create_failed"
    assert payload["status_code"] == 400
    assert payload["provider_code"] == "invalid_request"
    assert "Payment method" not in line
    assert "test_secretkey" not in line
