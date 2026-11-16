"""Telegram Stars: invoice, checkout, exactly-once credit, recovery, refund."""

from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import (
    StarTransaction,
    StarTransactions,
    TransactionPartnerUser,
    User,
)

from config import DAYS_PER_MONTH, stars_price
from services.billing import BillingError, BillingService, invoice_texts
from services.stars import CHECKOUT_ERRORS, StarsPayments, provider_id_for

RATE = Decimal("1.60")
BUYER = 515151


class FakeStarsBot:
    def __init__(self) -> None:
        self.transactions: list[StarTransaction] = []
        self.refunds: list[tuple[int, str]] = []
        self.fail_refund = False

    async def get_star_transactions(
        self, offset: int | None = None, limit: int | None = None
    ) -> StarTransactions:
        start = offset or 0
        return StarTransactions(
            transactions=self.transactions[start : start + (limit or 100)]
        )

    async def refund_star_payment(
        self, user_id: int, telegram_payment_charge_id: str
    ) -> bool:
        if self.fail_refund:
            raise RuntimeError("refund refused")
        self.refunds.append((user_id, telegram_payment_charge_id))
        return True


@pytest.fixture
def stars_bot() -> FakeStarsBot:
    return FakeStarsBot()


@pytest.fixture
def stars(services: Any, stars_bot: FakeStarsBot) -> StarsPayments:
    services.billing = BillingService(
        services.db,
        services.sbp,
        services.cryptopay,
        stars_rub_per_star=RATE,
    )
    payments = StarsPayments(
        services.db,
        services.provisioning,
        stars_bot,  # type: ignore[arg-type]
        services.settings.admin_telegram_id,
    )
    services.stars = payments
    return payments


async def _buyer(services: Any, telegram_id: int = BUYER) -> dict[str, Any]:
    await services.db.create_user(telegram_id, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(telegram_id)
    user = await services.db.get_user(telegram_id)
    assert user is not None
    return user


async def _order(services: Any, months: int = 1, tier: str = "standard") -> Any:
    return await services.billing.create(BUYER, months, "stars", tier)


def test_invoice_texts_fit_telegram_limits() -> None:
    for tier, months in (("standard", 6), ("complete", 12), ("basic", 1)):
        title, description, label = invoice_texts(tier, months)
        assert 1 <= len(title) <= 32
        assert 1 <= len(description) <= 255
        assert label
    assert "100 ГБ" in invoice_texts("standard", 1)[1]
    assert "до 2 устройств" in invoice_texts("standard", 1)[1]


async def test_order_is_priced_in_whole_stars_and_reused(
    services: Any, stars: StarsPayments
) -> None:
    await _buyer(services)
    first = await _order(services, 3)
    assert first.data["amount_xtr"] == stars_price("standard", 3, RATE) == 500
    stored = await services.db.get_payment(first.payment_id)
    assert (stored["currency"], stored["method"], stored["amount"]) == (
        "XTR",
        "stars",
        500,
    )
    # A repeated tap re-sends the same unpaid order instead of a new one.
    again = await _order(services, 3)
    assert again.payment_id == first.payment_id
    # One unpaid order at a time: another plan needs the first one cancelled.
    with pytest.raises(BillingError, match="too_many_pending"):
        await _order(services, 2)
    assert await services.db.cancel_own_payment(BUYER, first.payment_id)
    other = await _order(services, 2)
    assert other.payment_id != first.payment_id


async def test_stars_disabled_and_offer_are_enforced(services: Any) -> None:
    await services.db.create_user(BUYER, "buyer", None)
    with pytest.raises(BillingError, match="offer_not_accepted"):
        await services.billing.create(BUYER, 1, "stars", "standard")
    await services.provisioning.accept_offer_and_start_trial(BUYER)
    with pytest.raises(BillingError, match="stars_unavailable"):
        await services.billing.create(BUYER, 1, "stars", "standard")


async def test_checkout_validates_owner_amount_and_state(
    services: Any, stars: StarsPayments
) -> None:
    await _buyer(services)
    order = await _order(services)
    amount = order.data["amount_xtr"]
    check = stars.checkout_error
    assert (
        await check(
            payload=order.payment_id, from_id=BUYER, currency="XTR", total_amount=amount
        )
        is None
    )
    assert (
        await check(
            payload=order.payment_id, from_id=999, currency="XTR", total_amount=amount
        )
        == "not_found"
    )
    assert (
        await check(
            payload=order.payment_id,
            from_id=BUYER,
            currency="XTR",
            total_amount=amount + 1,
        )
        == "mismatch"
    )
    assert (
        await check(payload="../etc", from_id=BUYER, currency="XTR", total_amount=1)
        == "not_found"
    )
    await services.db.set_user_status(BUYER, "banned")
    assert (
        await check(
            payload=order.payment_id, from_id=BUYER, currency="XTR", total_amount=amount
        )
        == "banned"
    )
    assert set(CHECKOUT_ERRORS) >= {"not_found", "stale", "banned", "mismatch"}


async def test_stale_order_is_refused_at_checkout(
    services: Any, stars: StarsPayments
) -> None:
    await _buyer(services)
    order = await _order(services)
    await services.db._execute(
        "UPDATE payments SET created_at = ? WHERE payment_id = ?",
        (int(time.time()) - 49 * 3600, order.payment_id),
    )
    assert (
        await stars.checkout_error(
            payload=order.payment_id,
            from_id=BUYER,
            currency="XTR",
            total_amount=order.data["amount_xtr"],
        )
        == "stale"
    )


async def test_success_credits_once_with_the_tier_quota(
    services: Any, stars: StarsPayments, hiddify: Any
) -> None:
    user = await _buyer(services)
    order = await _order(services, 2)
    before = int(user["expires_at"])
    kwargs = {
        "payload": order.payment_id,
        "from_id": BUYER,
        "currency": "XTR",
        "total_amount": order.data["amount_xtr"],
        "charge_id": "stxCHARGE-1",
    }
    assert await stars.record_success(**kwargs)
    paid = await services.db.get_payment(order.payment_id)
    assert paid["status"] == "paid"
    assert paid["provider_id"] == provider_id_for("stxCHARGE-1")
    after = await services.db.get_user(BUYER)
    expected = before + 2 * DAYS_PER_MONTH * 86400
    assert abs(int(after["expires_at"]) - expected) <= 2
    assert after["tier"] == "standard"
    panel = hiddify.users[after["node_username"]]
    assert panel["data_limit"] == 100 * 1024**3
    assert panel["data_limit_reset_strategy"] == "month"

    # The same update delivered again never adds a second period.
    assert await stars.record_success(**kwargs)
    again = await services.db.get_user(BUYER)
    assert again["expires_at"] == after["expires_at"]
    # A different charge for an already paid order is refused.
    assert not await stars.record_success(**{**kwargs, "charge_id": "other"})


async def test_success_after_stale_cancel_still_credits(
    services: Any, stars: StarsPayments
) -> None:
    await _buyer(services)
    order = await _order(services)
    await services.db.cancel_confirmed_payment(order.payment_id)
    assert await stars.record_success(
        payload=order.payment_id,
        from_id=BUYER,
        currency="XTR",
        total_amount=order.data["amount_xtr"],
        charge_id="stx-late",
    )
    assert (await services.db.get_payment(order.payment_id))["status"] == "paid"


async def test_mismatched_success_is_not_credited_and_alerts(
    services: Any, stars: StarsPayments, notifier: Any
) -> None:
    await _buyer(services)
    order = await _order(services)
    assert not await stars.record_success(
        payload=order.payment_id,
        from_id=BUYER,
        currency="XTR",
        total_amount=1,
        charge_id="stx-wrong",
    )
    assert (await services.db.get_payment(order.payment_id))["status"] == "pending"
    assert any(
        target == services.settings.admin_telegram_id and "не совпала" in text
        for target, text in notifier.messages
    )


def _tx(tx_id: str, user_id: int, payload: str, amount: int) -> StarTransaction:
    return StarTransaction(
        id=tx_id,
        amount=amount,
        date=int(time.time()),
        source=TransactionPartnerUser(
            transaction_type="invoice_payment",
            user=User(id=user_id, is_bot=False, first_name="B"),
            invoice_payload=payload,
        ),
    )


async def test_reconcile_recovers_a_lost_success_update(
    services: Any, stars: StarsPayments, stars_bot: FakeStarsBot
) -> None:
    await _buyer(services)
    order = await _order(services)
    amount = order.data["amount_xtr"]
    stars_bot.transactions = [
        _tx("stx-foreign", 42, "f" * 32, 10),
        _tx("stx-lost", BUYER, order.payment_id, amount),
    ]
    assert await stars.reconcile() == 1
    paid = await services.db.get_payment(order.payment_id)
    assert paid["status"] == "paid"
    assert paid["provider_id"] == provider_id_for("stx-lost")
    # Running again changes nothing.
    assert await stars.reconcile() == 0
    assert await services.db.get_meta("stars_transactions_seen") == "2"


async def test_reconcile_ignores_amount_or_owner_mismatch(
    services: Any, stars: StarsPayments, stars_bot: FakeStarsBot
) -> None:
    await _buyer(services)
    order = await _order(services)
    stars_bot.transactions = [_tx("stx-bad", BUYER, order.payment_id, 1)]
    assert await stars.reconcile() == 0
    assert (await services.db.get_payment(order.payment_id))["status"] == "pending"


async def test_refund_returns_stars_and_takes_back_the_period(
    services: Any, stars: StarsPayments, stars_bot: FakeStarsBot
) -> None:
    user = await _buyer(services)
    trial_end = int(user["expires_at"])
    order = await _order(services, 1)
    await stars.record_success(
        payload=order.payment_id,
        from_id=BUYER,
        currency="XTR",
        total_amount=order.data["amount_xtr"],
        charge_id="stx-refund",
    )
    assert await stars.refund(order.payment_id) == "refunded"
    assert stars_bot.refunds == [(BUYER, "stx-refund")]
    payment = await services.db.get_payment(order.payment_id)
    assert payment["status"] == "refunded"
    after = await services.db.get_user(BUYER)
    assert abs(int(after["expires_at"]) - trial_end) <= 2
    # A second refund or Telegram's refund notice changes nothing more.
    assert await stars.refund(order.payment_id) == "not_refundable"
    assert not await stars.record_refund(payload=order.payment_id, from_id=BUYER)
    assert (await services.db.get_user(BUYER))["expires_at"] == after["expires_at"]


async def test_failed_telegram_refund_changes_nothing(
    services: Any, stars: StarsPayments, stars_bot: FakeStarsBot
) -> None:
    await _buyer(services)
    order = await _order(services)
    await stars.record_success(
        payload=order.payment_id,
        from_id=BUYER,
        currency="XTR",
        total_amount=order.data["amount_xtr"],
        charge_id="stx-keep",
    )
    stars_bot.fail_refund = True
    assert await stars.refund(order.payment_id) == "telegram_error"
    assert (await services.db.get_payment(order.payment_id))["status"] == "paid"


async def test_refund_of_whole_period_expires_the_node(
    services: Any, stars: StarsPayments, hiddify: Any
) -> None:
    await _buyer(services)
    order = await _order(services)
    await stars.record_success(
        payload=order.payment_id,
        from_id=BUYER,
        currency="XTR",
        total_amount=order.data["amount_xtr"],
        charge_id="stx-all",
    )
    # Pretend the trial had already ended before the paid month started.
    paid_end = int((await services.db.get_user(BUYER))["expires_at"])
    await services.db._execute(
        "UPDATE users SET expires_at = ? WHERE telegram_id = ?",
        (paid_end - 86400, BUYER),
    )
    await stars.refund(order.payment_id)
    user = await services.db.get_user(BUYER)
    assert user["status"] == "expired"
    assert hiddify.users[user["node_username"]]["status"] == "disabled"


# ----------------------------------------------------------------- handlers
async def test_pre_checkout_handler_answers_ok_and_refusal(
    services: Any, stars: StarsPayments
) -> None:
    from handlers.payments import stars_pre_checkout

    await _buyer(services)
    order = await _order(services)
    query = SimpleNamespace(
        invoice_payload=order.payment_id,
        from_user=SimpleNamespace(id=BUYER),
        currency="XTR",
        total_amount=order.data["amount_xtr"],
        answer=AsyncMock(),
    )
    await stars_pre_checkout(query, services)  # type: ignore[arg-type]
    query.answer.assert_awaited_once_with(ok=True)

    query.answer.reset_mock()
    query.total_amount += 5
    await stars_pre_checkout(query, services)  # type: ignore[arg-type]
    query.answer.assert_awaited_once_with(
        ok=False, error_message=CHECKOUT_ERRORS["mismatch"]
    )


async def test_pre_checkout_refuses_when_stars_are_off(services: Any) -> None:
    from handlers.payments import stars_pre_checkout

    query = SimpleNamespace(
        invoice_payload="a" * 32,
        from_user=SimpleNamespace(id=BUYER),
        currency="XTR",
        total_amount=1,
        answer=AsyncMock(),
    )
    await stars_pre_checkout(query, services)  # type: ignore[arg-type]
    assert query.answer.await_args.kwargs["ok"] is False


async def test_successful_payment_handler_credits(
    services: Any, stars: StarsPayments
) -> None:
    from handlers.payments import stars_successful_payment

    await _buyer(services)
    order = await _order(services)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=BUYER),
        successful_payment=SimpleNamespace(
            currency="XTR",
            total_amount=order.data["amount_xtr"],
            invoice_payload=order.payment_id,
            telegram_payment_charge_id="stx-handler",
            subscription_expiration_date=None,
        ),
        answer=AsyncMock(),
    )
    await stars_successful_payment(message, services)  # type: ignore[arg-type]
    assert (await services.db.get_payment(order.payment_id))["status"] == "paid"
    message.answer.assert_not_awaited()


async def test_invoice_is_sent_in_stars(services: Any, stars: StarsPayments) -> None:
    from handlers.payments import create_and_show

    await _buyer(services)
    message = MagicMock()
    message.answer_invoice = AsyncMock()
    message.answer = AsyncMock()
    await create_and_show(message, BUYER, "stars", 4, services, AsyncMock(), "standard")
    kwargs = message.answer_invoice.await_args.kwargs
    assert kwargs["currency"] == "XTR"
    assert kwargs["prices"][0].amount == stars_price("standard", 4, RATE)
    assert "provider_token" not in kwargs
    payment = await services.db.get_payment(kwargs["payload"])
    assert payment["months"] == 4


async def test_payment_messages_bypass_throttling_and_ban() -> None:
    from middlewares.banned import is_allowed_for_banned
    from middlewares.throttling import ThrottlingMiddleware
    from services.security import SlidingWindowLimiter

    middleware = ThrottlingMiddleware(SlidingWindowLimiter(limit=0, window_seconds=60))
    handler = AsyncMock(return_value="ok")
    from aiogram.types import Message

    event = MagicMock(spec=Message)
    event.successful_payment = SimpleNamespace(total_amount=1)
    event.refunded_payment = None
    user = User(id=1, is_bot=False, first_name="A")
    assert await middleware(handler, event, {"event_from_user": user}) == "ok"
    assert is_allowed_for_banned(event)


def test_bot_menu_lists_stars_first_when_enabled(services: Any) -> None:
    from handlers.payments import bot_methods, channel_text
    from keyboards.main_menu import channel_kb, tariffs_kb

    services.settings = services.settings.model_copy(update={"stars_enabled": True})
    services.billing = BillingService(
        services.db,
        services.sbp,
        services.cryptopay,
        allow_legacy_sbp=True,
        stars_rub_per_star=RATE,
    )
    methods = bot_methods(services)
    assert methods == ["stars", "sbp"]
    rows = channel_kb(methods=methods).inline_keyboard
    assert [row[0].callback_data for row in rows] == ["chan:stars", "chan:sbp"]
    assert "Telegram Stars" in channel_text(methods)
    periods = tariffs_kb("stars", "standard", RATE).inline_keyboard
    assert [row[0].callback_data for row in periods[:-1]] == [
        f"pay:stars:standard:{m}" for m in range(1, 7)
    ]
    assert "187 ⭐" in str(periods[0][0].text)

    services.settings = services.settings.model_copy(update={"yookassa_in_bot": False})
    assert bot_methods(services) == ["stars"]
