"""YooKassa inside Telegram (BotFather token): invoice, checkout, one credit."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import DAYS_PER_MONTH, Settings
from services.billing import BillingError
from services.sbp import SbpService
from services.telegram_rub import (
    CHECKOUT_ERRORS,
    TelegramRubPayments,
    invoice_options,
    payload_for,
)

BUYER = 818181
LIVE_TOKEN = "390540012:LIVE:12345"


@pytest.fixture
def rub(services: Any) -> TelegramRubPayments:
    payments = TelegramRubPayments(
        services.db,
        services.provisioning,
        services.settings.admin_telegram_id,
        LIVE_TOKEN,
    )
    services.telegram_rub = payments
    return payments


async def _buyer(services: Any) -> dict[str, Any]:
    await services.db.create_user(BUYER, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(BUYER)
    user = await services.db.get_user(BUYER)
    assert user is not None
    return user


async def _order(services: Any, months: int = 1, tier: str = "standard") -> Any:
    return await services.billing.create_telegram_rub(BUYER, months, tier)


def test_receipt_fields_only_for_fiscal_mode() -> None:
    kwargs = {
        "amount_kopecks": 29900,
        "months": 1,
        "tier_title": "Стандарт",
        "vat_code": 1,
        "tax_system_code": 2,
    }
    assert invoice_options(receipt_mode="self_employed", **kwargs) == {}
    fiscal = invoice_options(receipt_mode="fiscal", **kwargs)
    assert fiscal["need_email"] is True
    assert fiscal["send_email_to_provider"] is True
    receipt = json.loads(fiscal["provider_data"])["receipt"]
    item = receipt["items"][0]
    assert item["amount"] == {"value": "299.00", "currency": "RUB"}
    assert item["quantity"] == "1.00"
    assert (item["vat_code"], item["payment_subject"]) == (1, "service")
    assert receipt["tax_system_code"] == 2


def test_token_format_is_checked(settings: Settings) -> None:
    base = settings.model_dump()
    ok = Settings(**{**base, "telegram_payment_token": LIVE_TOKEN})
    assert ok.telegram_payment_token is not None
    assert (
        Settings(**{**base, "telegram_payment_token": "  "}).telegram_payment_token
        is None
    )
    with pytest.raises(ValueError, match="telegram_payment_token"):
        Settings(**{**base, "telegram_payment_token": "live_abcdef secret"})


async def test_order_is_in_kopecks_and_reused(
    services: Any, rub: TelegramRubPayments
) -> None:
    await _buyer(services)
    first = await _order(services, 3)
    assert first.data["amount_kopecks"] == 79900
    stored = await services.db.get_payment(first.payment_id)
    assert (stored["method"], stored["currency"], stored["channel"]) == (
        "sbp",
        "RUB",
        "telegram",
    )
    assert (await _order(services, 3)).payment_id == first.payment_id
    with pytest.raises(BillingError, match="too_many_pending"):
        await _order(services, 2)
    assert await services.db.cancel_own_payment(BUYER, first.payment_id)
    assert (await _order(services, 2)).payment_id != first.payment_id
    with pytest.raises(BillingError, match="invalid_months"):
        await services.billing.create_telegram_rub(BUYER, 12, "standard")


async def test_checkout_rules(services: Any, rub: TelegramRubPayments) -> None:
    await _buyer(services)
    order = await _order(services)
    payload = payload_for(order.payment_id)
    amount = order.data["amount_kopecks"]

    async def check(**changes: Any) -> str | None:
        kwargs = {
            "payload": payload,
            "from_id": BUYER,
            "currency": "RUB",
            "total_amount": amount,
            **changes,
        }
        return await rub.checkout_error(**kwargs)

    assert await check() is None
    assert await check(from_id=1) == "not_found"
    assert await check(total_amount=amount - 100) == "mismatch"
    assert await check(currency="XTR") == "mismatch"
    assert await check(payload=order.payment_id) == "not_found"  # no yk: prefix
    await services.db._execute(
        "UPDATE payments SET created_at = ? WHERE payment_id = ?",
        (int(time.time()) - 49 * 3600, order.payment_id),
    )
    assert await check() == "stale"
    assert set(CHECKOUT_ERRORS) >= {"not_found", "stale", "mismatch", "banned"}


async def test_payment_extends_once_and_keeps_yookassa_number(
    services: Any, rub: TelegramRubPayments
) -> None:
    user = await _buyer(services)
    order = await _order(services, 2)
    kwargs = {
        "payload": payload_for(order.payment_id),
        "from_id": BUYER,
        "currency": "RUB",
        "total_amount": order.data["amount_kopecks"],
        "provider_charge_id": "2f3a-yookassa-payment",
    }
    assert await rub.record_success(**kwargs)
    assert await rub.record_success(**kwargs)  # same update again
    paid = await services.db.get_payment(order.payment_id)
    assert paid["status"] == "paid"
    assert paid["provider_id"] == "tgyk:2f3a-yookassa-payment"
    after = await services.db.get_user(BUYER)
    expected = int(user["expires_at"]) + 2 * DAYS_PER_MONTH * 86400
    assert abs(int(after["expires_at"]) - expected) <= 2
    assert not await rub.record_success(**{**kwargs, "provider_charge_id": "other"})
    # Bound orders survive the stale-order cleanup.
    assert await services.db.cancel_stale_payments(int(time.time()) + 10) == 0


async def test_late_payment_of_a_cancelled_order_is_credited(
    services: Any, rub: TelegramRubPayments
) -> None:
    await _buyer(services)
    order = await _order(services)
    await services.db.cancel_confirmed_payment(order.payment_id)
    assert await rub.record_success(
        payload=payload_for(order.payment_id),
        from_id=BUYER,
        currency="RUB",
        total_amount=order.data["amount_kopecks"],
        provider_charge_id="late-1",
    )
    assert (await services.db.get_payment(order.payment_id))["status"] == "paid"


async def test_wrong_amount_alerts_owner_and_credits_nothing(
    services: Any, rub: TelegramRubPayments, notifier: Any
) -> None:
    await _buyer(services)
    order = await _order(services)
    assert not await rub.record_success(
        payload=payload_for(order.payment_id),
        from_id=BUYER,
        currency="RUB",
        total_amount=100,
        provider_charge_id="bad-1",
    )
    assert (await services.db.get_payment(order.payment_id))["status"] == "pending"
    assert any("не совпала" in text for _, text in notifier.messages)


# ----------------------------------------------------------------- handlers
def _message() -> Any:
    message = MagicMock()
    message.answer = AsyncMock()
    message.answer_invoice = AsyncMock()
    return message


async def test_new_ruble_orders_never_open_a_card_checkout(
    services: Any, rub: TelegramRubPayments
) -> None:
    """Rubles are SBP only: a Telegram invoice would ask for a bank card."""
    from handlers.payments import create_and_show

    await _buyer(services)
    message = AsyncMock()
    await create_and_show(message, BUYER, "sbp", 4, services, AsyncMock(), "standard")
    message.answer_invoice.assert_not_awaited()
    assert message.answer.await_count + message.answer_photo.await_count > 0


async def test_pre_checkout_and_success_handlers(
    services: Any, rub: TelegramRubPayments
) -> None:
    from handlers.payments import stars_pre_checkout, stars_successful_payment

    await _buyer(services)
    order = await _order(services)
    query = SimpleNamespace(
        invoice_payload=payload_for(order.payment_id),
        from_user=SimpleNamespace(id=BUYER),
        currency="RUB",
        total_amount=order.data["amount_kopecks"],
        answer=AsyncMock(),
    )
    await stars_pre_checkout(query, services)  # type: ignore[arg-type]
    query.answer.assert_awaited_once_with(ok=True)

    message = SimpleNamespace(
        from_user=SimpleNamespace(id=BUYER),
        successful_payment=SimpleNamespace(
            currency="RUB",
            total_amount=order.data["amount_kopecks"],
            invoice_payload=payload_for(order.payment_id),
            telegram_payment_charge_id="tg-1",
            provider_payment_charge_id="yk-777",
            subscription_expiration_date=None,
        ),
        answer=AsyncMock(),
    )
    await stars_successful_payment(message, services)  # type: ignore[arg-type]
    assert (await services.db.get_payment(order.payment_id))["status"] == "paid"
    message.answer.assert_not_awaited()

    query.answer.reset_mock()
    await stars_pre_checkout(query, services)  # paid already
    assert query.answer.await_args.kwargs["ok"] is False


async def test_admin_refund_takes_back_the_period(
    services: Any, rub: TelegramRubPayments
) -> None:
    from aiogram.filters import CommandObject

    from handlers.admin import cmd_refund

    user = await _buyer(services)
    order = await _order(services)
    await rub.record_success(
        payload=payload_for(order.payment_id),
        from_id=BUYER,
        currency="RUB",
        total_amount=order.data["amount_kopecks"],
        provider_charge_id="yk-refund-1",
    )
    message = _message()
    message.from_user = SimpleNamespace(id=1)
    await cmd_refund(
        message, CommandObject(command="refund", args=order.payment_id), services
    )
    assert "yk-refund-1" in message.answer.await_args.args[0]
    assert (await services.db.get_payment(order.payment_id))["status"] == "refunded"
    after = await services.db.get_user(BUYER)
    assert abs(int(after["expires_at"]) - int(user["expires_at"])) <= 2


# ------------------------------------------------------- placeholder links
async def test_placeholder_sbp_links_are_never_shown(services: Any) -> None:
    from handlers.payments import bot_methods

    placeholder = "https://qr.nspk.ru/REPLACE_WITH_YOUR_QR"
    services.billing.sbp = SbpService("manual", placeholder, placeholder, None)
    services.billing.yookassa = None
    assert "sbp" not in bot_methods(services)
    await _buyer(services)
    with pytest.raises(BillingError, match="ruble_payments_unavailable"):
        await services.billing.create(BUYER, 1, "sbp", "standard")
