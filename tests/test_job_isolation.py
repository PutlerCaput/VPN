"""One bad record must not stop a background job for everyone after it."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import services.payment_reconciliation as reconciliation
import services.scheduler as scheduler_module
from services.notifier import Notifier
from services.payment_reconciliation import PaymentReconciler
from services.scheduler import Jobs

DAY = 86400
USER = 990001


async def test_failed_expiry_does_not_hold_back_the_next_users(services: Any) -> None:
    users = [{"telegram_id": 1}, {"telegram_id": 2}, {"telegram_id": 3}]
    db = SimpleNamespace(users_to_expire=AsyncMock(return_value=users))
    provisioning = SimpleNamespace(
        expire_user=AsyncMock(
            side_effect=[RuntimeError("database is locked"), None, None]
        )
    )
    jobs = Jobs(
        cast(Any, db),
        cast(Any, provisioning),
        cast(Notifier, services.provisioning.notifier),
        services.monitor,
        services.settings,
        [],
    )

    await jobs.daily_expiry_check()

    assert provisioning.expire_user.await_count == 3


async def test_failed_reminder_is_retried_and_does_not_skip_the_rest(
    db: Any, services: Any, monkeypatch: Any
) -> None:
    current = int(time.time())
    monkeypatch.setattr(scheduler_module, "time", SimpleNamespace(time=lambda: current))
    for telegram_id in (991001, 991002):
        await db.create_user(telegram_id, "user", None)
        await db.set_subscription(
            telegram_id,
            status="active",
            expires_at=current + 3 * DAY,
            node_username=f"u{telegram_id}",
            config_link=None,
        )
        await db._execute(
            "UPDATE users SET created_at = ? WHERE telegram_id = ?",
            (current - 60, telegram_id),
        )

    class FirstCallFails:
        def __init__(self) -> None:
            self.calls: list[int] = []

        async def notify(self, telegram_id: int, text: str, **_: Any) -> bool:
            self.calls.append(telegram_id)
            if len(self.calls) == 1:
                raise RuntimeError("unexpected")
            return True

    notifier = FirstCallFails()
    jobs = Jobs(
        db,
        services.provisioning,
        cast(Notifier, notifier),
        services.monitor,
        services.settings,
        [],
    )

    await jobs.expiry_reminder()
    failed, delivered = notifier.calls
    assert (await db.get_user(delivered))["expiry_reminded_for"] == current + 3 * DAY
    assert (await db.get_user(failed))["expiry_reminded_for"] is None

    await jobs.expiry_reminder()
    assert notifier.calls[2:] == [failed]


async def test_unexpected_error_in_one_check_does_not_cancel_the_others(
    services: Any, monkeypatch: Any
) -> None:
    await services.db.create_user(USER, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(USER)
    for payment_id in ("order-a", "order-b"):
        await services.db.create_payment(
            payment_id=payment_id,
            telegram_id=USER,
            amount=30000,
            currency="RUB",
            months=1,
            method="sbp",
            provider_id=f"yookassa:{payment_id}",
        )
    checked: list[str] = []

    async def reconcile(
        db: Any, client: Any, provisioning: Any, payment: dict[str, Any]
    ) -> str:
        checked.append(payment["payment_id"])
        if payment["payment_id"] == "order-a":
            raise RuntimeError("unexpected")
        return "pending"

    monkeypatch.setattr(reconciliation, "reconcile_yookassa", reconcile)
    reconciler = PaymentReconciler(
        services.db, services.provisioning, cast(Any, object()), None
    )

    await reconciler.poll()

    assert sorted(checked) == ["order-a", "order-b"]
