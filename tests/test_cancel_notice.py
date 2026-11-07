"""A client hears that auto-renewal is off, and until when the paid period lasts."""

from __future__ import annotations

import time
from typing import Any

import pytest

from services.billing import BillingService
from services.provisioning import format_date
from services.stars import StarsPayments, subscription_payload
from tests.conftest import FakeNotifier
from tests.test_autorenew import BUYER, RATE, SubscriptionBot, _charge, _subscribed

NOTICE = "Автопродление отключено"


@pytest.fixture
def stars(services: Any) -> StarsPayments:
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
        SubscriptionBot(),  # type: ignore[arg-type]
        services.settings.admin_telegram_id,
    )
    services.stars = payments
    return payments


def _notices(notifier: FakeNotifier) -> list[str]:
    return [text for tid, text in notifier.messages if tid == BUYER and NOTICE in text]


async def test_cancellation_tells_the_client_the_paid_date(
    services: Any, stars: StarsPayments, notifier: FakeNotifier
) -> None:
    sub_id, amount = await _subscribed(services)
    paid_until = int(time.time()) + 3600
    await _charge(stars, sub_id, amount, "stx-1", next_charge_at=paid_until)
    payload = subscription_payload(sub_id)
    await stars.subscription_changed(payload=payload, from_id=BUYER, state="canceled")
    user = await services.db.get_user(BUYER)
    assert user is not None
    notices = _notices(notifier)
    assert len(notices) == 1
    assert format_date(int(user["expires_at"])) in notices[0]
    # Telegram may report the same cancellation again: the client hears it once.
    await stars.subscription_changed(payload=payload, from_id=BUYER, state="canceled")
    assert len(_notices(notifier)) == 1


async def test_cancellation_made_in_the_bot_is_not_announced_twice(
    services: Any, stars: StarsPayments, notifier: FakeNotifier
) -> None:
    sub_id, amount = await _subscribed(services)
    await _charge(
        stars, sub_id, amount, "stx-1", next_charge_at=int(time.time()) + 3600
    )
    assert await stars.cancel_subscription(sub_id)
    # Telegram's echo of the bot's own cancellation: already recorded, no notice.
    await stars.subscription_changed(
        payload=subscription_payload(sub_id), from_id=BUYER, state="canceled"
    )
    assert _notices(notifier) == []
