"""Monthly Telegram Stars subscriptions: each charge extends exactly once."""

from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import StarTransaction, TransactionPartnerUser, User

from config import DAYS_PER_MONTH
from services.billing import BillingError, BillingService
from services.stars import (
    SUBSCRIPTION_PERIOD_SECONDS,
    StarsPayments,
    provider_id_for,
    subscription_payload,
)
from tests.test_stars import FakeStarsBot

RATE = Decimal("1.60")
BUYER = 626262
MONTH = DAYS_PER_MONTH * 86400


class SubscriptionBot(FakeStarsBot):
    def __init__(self) -> None:
        super().__init__()
        self.cancels: list[tuple[int, str]] = []
        self.fail_cancel = False

    async def edit_user_star_subscription(
        self, user_id: int, telegram_payment_charge_id: str, is_canceled: bool
    ) -> bool:
        if self.fail_cancel:
            raise RuntimeError("refused")
        self.cancels.append((user_id, telegram_payment_charge_id))
        return True


@pytest.fixture
def bot() -> SubscriptionBot:
    return SubscriptionBot()


@pytest.fixture
def stars(services: Any, bot: SubscriptionBot) -> StarsPayments:
    services.settings = services.settings.model_copy(
        update={
            "stars_enabled": True,
            "stars_autorenew_enabled": True,
            "enabled_tiers_csv": "standard",
        }
    )
    services.billing = BillingService(
        services.db,
        services.sbp,
        services.cryptopay,
        stars_rub_per_star=RATE,
    )
    payments = StarsPayments(
        services.db,
        services.provisioning,
        bot,  # type: ignore[arg-type]
        services.settings.admin_telegram_id,
    )
    services.stars = payments
    return payments


async def _subscribed(services: Any) -> tuple[str, int]:
    await services.db.create_user(BUYER, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(BUYER)
    created = await services.billing.create_stars_subscription(BUYER, "standard")
    return created.payment_id, int(created.data["amount_xtr"])


async def _charge(
    stars: StarsPayments, sub_id: str, amount: int, charge: str, **extra: Any
) -> bool:
    return await stars.record_success(
        payload=subscription_payload(sub_id),
        from_id=BUYER,
        currency="XTR",
        total_amount=amount,
        charge_id=charge,
        **extra,
    )


async def test_subscription_offer_uses_the_monthly_price(
    services: Any, stars: StarsPayments
) -> None:
    sub_id, amount = await _subscribed(services)
    assert amount == 187
    sub = await services.db.get_star_subscription(sub_id)
    assert (sub["status"], sub["tier"], sub["amount"]) == ("pending", "standard", 187)
    # A second tap reuses the unpaid subscription.
    again = await services.billing.create_stars_subscription(BUYER, "standard")
    assert again.payment_id == sub_id
    assert (
        await stars.checkout_error(
            payload=subscription_payload(sub_id),
            from_id=BUYER,
            currency="XTR",
            total_amount=amount,
        )
        is None
    )
    assert (
        await stars.checkout_error(
            payload=subscription_payload(sub_id),
            from_id=BUYER,
            currency="XTR",
            total_amount=amount - 1,
        )
        == "mismatch"
    )


async def test_every_renewal_adds_thirty_days_once(
    services: Any, stars: StarsPayments
) -> None:
    sub_id, amount = await _subscribed(services)
    start = int((await services.db.get_user(BUYER))["expires_at"])
    next_at = int(time.time()) + SUBSCRIPTION_PERIOD_SECONDS
    assert await _charge(stars, sub_id, amount, "stx-1", next_charge_at=next_at)
    assert await _charge(stars, sub_id, amount, "stx-1", next_charge_at=next_at)
    after_first = int((await services.db.get_user(BUYER))["expires_at"])
    assert abs(after_first - (start + MONTH)) <= 2
    assert await _charge(stars, sub_id, amount, "stx-2")
    after_second = int((await services.db.get_user(BUYER))["expires_at"])
    assert abs(after_second - (start + 2 * MONTH)) <= 2

    sub = await services.db.get_star_subscription(sub_id)
    assert sub["status"] == "active"
    assert (sub["first_charge_id"], sub["last_charge_id"]) == ("stx-1", "stx-2")
    rows = await services.db._fetchall(
        "SELECT status, subscription_id FROM payments WHERE subscription_id = ?",
        (sub_id,),
    )
    assert [r["status"] for r in rows] == ["paid", "paid"]
    with pytest.raises(BillingError, match="subscription_exists"):
        await services.billing.create_stars_subscription(BUYER, "standard")


async def test_foreign_or_wrong_amount_charge_is_not_credited(
    services: Any, stars: StarsPayments, notifier: Any
) -> None:
    sub_id, amount = await _subscribed(services)
    assert not await _charge(stars, sub_id, amount + 5, "stx-bad")
    assert not await stars.record_success(
        payload=subscription_payload(sub_id),
        from_id=999,
        currency="XTR",
        total_amount=amount,
        charge_id="stx-other",
    )
    assert (await services.db.get_star_subscription(sub_id))["status"] == "pending"
    assert any("не совпало" in text for _, text in notifier.messages)


async def test_user_cancel_and_failed_charge_updates(
    services: Any, stars: StarsPayments, bot: SubscriptionBot
) -> None:
    sub_id, amount = await _subscribed(services)
    await _charge(stars, sub_id, amount, "stx-1")
    payload = subscription_payload(sub_id)
    assert (
        await stars.subscription_changed(payload=payload, from_id=BUYER, state="failed")
        == "failed"
    )
    assert (await stars.active_subscription(BUYER))["status"] == "failed"
    assert (
        await stars.subscription_changed(payload=payload, from_id=1, state="canceled")
        is None
    )
    assert (
        await stars.subscription_changed(
            payload=payload, from_id=BUYER, state="canceled"
        )
        == "canceled"
    )
    assert await stars.active_subscription(BUYER) is None
    # The bot can also cancel on the user's request.
    await stars.subscription_changed(payload=payload, from_id=BUYER, state="active")
    assert await stars.cancel_subscription(sub_id)
    assert bot.cancels == [(BUYER, "stx-1")]
    assert (await services.db.get_star_subscription(sub_id))["status"] == "canceled"


async def test_failed_cancel_keeps_the_subscription(
    services: Any, stars: StarsPayments, bot: SubscriptionBot
) -> None:
    sub_id, amount = await _subscribed(services)
    await _charge(stars, sub_id, amount, "stx-1")
    bot.fail_cancel = True
    assert not await stars.cancel_subscription(sub_id)
    assert (await services.db.get_star_subscription(sub_id))["status"] == "active"


async def test_reconcile_recovers_a_lost_renewal(
    services: Any, stars: StarsPayments, bot: SubscriptionBot
) -> None:
    sub_id, amount = await _subscribed(services)
    await _charge(stars, sub_id, amount, "stx-1")
    bot.transactions = [
        StarTransaction(
            id=tx_id,
            amount=amount,
            date=int(time.time()),
            source=TransactionPartnerUser(
                transaction_type="invoice_payment",
                user=User(id=BUYER, is_bot=False, first_name="B"),
                invoice_payload=subscription_payload(sub_id),
                subscription_period=SUBSCRIPTION_PERIOD_SECONDS,
            ),
        )
        for tx_id in ("stx-1", "stx-2")
    ]
    assert await stars.reconcile() == 1
    assert await services.db.get_payment_by_provider(provider_id_for("stx-2"))
    assert await stars.reconcile() == 0


async def test_refund_of_a_renewal_also_stops_the_subscription(
    services: Any, stars: StarsPayments, bot: SubscriptionBot
) -> None:
    sub_id, amount = await _subscribed(services)
    await _charge(stars, sub_id, amount, "stx-1")
    payment = await services.db.get_payment_by_provider(provider_id_for("stx-1"))
    assert await stars.refund(str(payment["payment_id"])) == "refunded"
    assert bot.refunds == [(BUYER, "stx-1")]
    assert (await services.db.get_star_subscription(sub_id))["status"] == "canceled"


async def test_banned_user_charge_cancels_and_alerts(
    services: Any, stars: StarsPayments, bot: SubscriptionBot, notifier: Any
) -> None:
    sub_id, amount = await _subscribed(services)
    await _charge(stars, sub_id, amount, "stx-1")
    await services.db.set_user_status(BUYER, "banned")
    assert not await _charge(stars, sub_id, amount, "stx-2")
    assert bot.cancels
    assert any("Заблокированному" in text for _, text in notifier.messages)
    assert await services.db.get_payment_by_provider(provider_id_for("stx-2")) is None


async def test_lapsed_subscription_ends(services: Any, stars: StarsPayments) -> None:
    sub_id, amount = await _subscribed(services)
    await _charge(stars, sub_id, amount, "stx-1", next_charge_at=1000)
    assert await stars.end_lapsed() == 1
    assert (await services.db.get_star_subscription(sub_id))["status"] == "ended"


# ----------------------------------------------------------------- handlers
async def test_autorenew_button_sends_a_subscription_link(
    services: Any, stars: StarsPayments
) -> None:
    from handlers.payments import cb_autorenew
    from keyboards.main_menu import tariffs_kb

    rows = tariffs_kb("stars", "standard", RATE, autorenew=True).inline_keyboard
    assert rows[0][0].callback_data == "autorenew:standard"
    assert "187 ⭐" in str(rows[0][0].text)
    assert "autorenew" not in str(tariffs_kb("sbp", "standard", autorenew=True))

    await services.db.create_user(BUYER, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(BUYER)
    services.bot.create_invoice_link = AsyncMock(return_value="https://t.me/$abc")
    message = MagicMock()
    message.answer = AsyncMock()
    from aiogram.types import Message

    message.__class__ = Message  # isinstance check in the handler
    callback = SimpleNamespace(
        data="autorenew:standard",
        message=message,
        from_user=SimpleNamespace(id=BUYER),
        answer=AsyncMock(),
    )
    await cb_autorenew(callback, services)  # type: ignore[arg-type]
    kwargs = services.bot.create_invoice_link.await_args.kwargs
    assert kwargs["subscription_period"] == 2592000
    assert kwargs["currency"] == "XTR"
    assert kwargs["payload"].startswith("sub:")
    assert kwargs["prices"][0].amount == 187
    markup = message.answer.await_args.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].url == "https://t.me/$abc"


async def test_reminder_mentions_active_autorenew(
    services: Any, stars: StarsPayments, notifier: Any
) -> None:
    from services.scheduler import Jobs

    sub_id, amount = await _subscribed(services)
    await _charge(stars, sub_id, amount, "stx-1")
    user = await services.db.get_user(BUYER)
    await services.db._execute(
        "UPDATE users SET expires_at = ?, created_at = ? WHERE telegram_id = ?",
        (int(time.time()) + 2 * 86400, int(time.time()) - 40 * 86400, BUYER),
    )
    jobs = Jobs(
        services.db,
        services.provisioning,
        notifier,
        services.monitor,
        services.settings,
        [],
        stars=stars,
    )
    await jobs.expiry_reminder()
    assert user is not None
    assert "Автопродление через Telegram Stars включено" in notifier.messages[-1][1]
