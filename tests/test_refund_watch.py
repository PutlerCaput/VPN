"""A refund made in the YooKassa dashboard takes the paid period back by itself."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.filters import CommandObject

from handlers.admin import cmd_refund, cmd_user
from services.refund_watch import (
    DAY,
    UNREACHABLE_RETRY,
    RefundWatch,
    check_interval,
    refunded_kopecks,
    rubles,
)
from services.scheduler import Jobs, build_scheduler
from services.yookassa import YooKassaError, YooKassaRejected

YID = "2f3a9c1e-000f-5000-8000-1a2b3c4d5e6f"
USER = 981001
ADMIN = 1
PAID = 29900
ORDER = "ab" * 16  # the bot issues 32 hex characters


class FakeYooKassa:
    def __init__(self) -> None:
        self.payments: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.fail: Exception | None = None

    def pay(self, refunded: str | None = None, **changes: Any) -> None:
        remote: dict[str, Any] = {
            "id": YID,
            "status": "succeeded",
            "paid": True,
            "amount": {"value": "299.00", "currency": "RUB"},
        }
        if refunded is not None:
            remote["refunded_amount"] = {"value": refunded, "currency": "RUB"}
        self.payments[YID] = {**remote, **changes}

    async def get_payment(self, yookassa_id: str) -> dict[str, Any]:
        self.calls.append(yookassa_id)
        if self.fail is not None:
            raise self.fail
        if yookassa_id not in self.payments:
            raise YooKassaRejected("get_payment: HTTP 404", status_code=404)
        return self.payments[yookassa_id]


@pytest.fixture
def yk() -> FakeYooKassa:
    return FakeYooKassa()


@pytest.fixture
def watch(services: Any, yk: FakeYooKassa) -> RefundWatch:
    return RefundWatch(
        services.db,
        yk,  # type: ignore[arg-type]
        services.provisioning,
        services.notifier,
        ADMIN,
    )


async def paid_order(services: Any, provider: str = "yookassa:") -> dict[str, Any]:
    """A customer with a trial who paid one month; returns the user as it is now."""
    await services.db.create_user(USER, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(USER)
    await services.db.create_payment(
        payment_id=ORDER,
        telegram_id=USER,
        amount=PAID,
        currency="RUB",
        months=1,
        method="sbp",
        provider_id=f"{provider}{YID}",
    )
    await services.provisioning.process_paid_payment(ORDER)
    user = await services.db.get_user(USER)
    assert user is not None
    return user


def texts(notifier: Any, to: int) -> list[str]:
    return [text for who, text in notifier.messages if who == to]


def forget_schedule(watch: RefundWatch) -> None:
    """Pretend the time to ask again has come."""
    watch._next.clear()


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize(
    ("remote", "kopecks"),
    [
        ({}, 0),
        ({"refunded_amount": None}, 0),
        ({"refunded_amount": {"value": "299.00", "currency": "RUB"}}, 29900),
        ({"refunded_amount": {"value": "0.50", "currency": "RUB"}}, 50),
        ({"refunded_amount": {"value": "299.00", "currency": "USD"}}, 0),
        ({"refunded_amount": {"value": "abc", "currency": "RUB"}}, 0),
        ({"refunded_amount": {"value": "-5.00", "currency": "RUB"}}, 0),
        ({"refunded_amount": {"value": "1.005", "currency": "RUB"}}, 0),
        ({"refunded_amount": "299.00"}, 0),
    ],
)
def test_the_returned_sum_is_read_strictly(
    remote: dict[str, Any], kopecks: int
) -> None:
    assert refunded_kopecks(remote) == kopecks


def test_orders_are_asked_about_often_at_first_and_rarely_later() -> None:
    assert check_interval(3600) == 600
    assert check_interval(5 * DAY) == 3600
    assert check_interval(100 * DAY) == DAY
    assert rubles(29900) == "299,00"


# ------------------------------------------------------------- full refund
@pytest.mark.parametrize("provider", ["yookassa:", "tgyk:"])
async def test_a_full_refund_takes_the_period_back_once(
    services: Any, notifier: Any, yk: FakeYooKassa, watch: RefundWatch, provider: str
) -> None:
    before = await paid_order(services, provider)
    yk.pay(refunded="299.00")

    assert await watch.run() == 1

    payment = await services.db.get_payment(ORDER)
    assert payment["status"] == "refunded"
    after = await services.db.get_user(USER)
    assert abs((int(before["expires_at"]) - int(after["expires_at"])) - 30 * DAY) <= 2
    assert any("↩️ Платёж возвращён" in t for t in texts(notifier, USER))
    admin = texts(notifier, ADMIN)
    assert len(admin) == 1
    for part in (ORDER, "полностью", "299,00"):
        assert part in admin[0]

    forget_schedule(watch)
    assert await watch.run() == 0  # nothing paid is left to ask about
    assert len(texts(notifier, ADMIN)) == 1
    assert yk.calls == [YID]


async def test_a_refund_that_ends_the_subscription_expires_the_user(
    services: Any, yk: FakeYooKassa, watch: RefundWatch
) -> None:
    await paid_order(services)
    # The trial is long over: the only time left is the paid month.
    user = await services.db.get_user(USER)
    await services.db.set_subscription(
        USER,
        status="active",
        expires_at=int(time.time()) + 30 * DAY,
        node_username=user["node_username"],
        config_link=None,
    )
    yk.pay(refunded="299.00")
    assert await watch.run() == 1
    user = await services.db.get_user(USER)
    assert user["status"] == "expired"


# --------------------------------------------------------- nothing to do
async def test_an_order_without_a_refund_is_left_alone_and_not_asked_again_soon(
    services: Any, notifier: Any, yk: FakeYooKassa, watch: RefundWatch
) -> None:
    await paid_order(services)
    yk.pay()
    assert await watch.run() == 0
    assert await watch.run() == 0  # ten minutes have not passed
    assert yk.calls == [YID]
    assert (await services.db.get_payment(ORDER))["status"] == "paid"
    assert texts(notifier, ADMIN) == []
    forget_schedule(watch)
    await watch.run()
    assert yk.calls == [YID, YID]


async def test_a_partial_refund_only_tells_the_owner_once(
    services: Any, notifier: Any, yk: FakeYooKassa, watch: RefundWatch
) -> None:
    before = await paid_order(services)
    yk.pay(refunded="100.00")
    assert await watch.run() == 0
    forget_schedule(watch)
    assert await watch.run() == 0
    assert (await services.db.get_payment(ORDER))["status"] == "paid"
    assert (await services.db.get_user(USER))["expires_at"] == before["expires_at"]
    admin = texts(notifier, ADMIN)
    assert len(admin) == 1
    assert "100,00 из 299,00" in admin[0]
    assert f"/refund {ORDER}" in admin[0]
    # More money going back is news again.
    yk.pay(refunded="150.00")
    forget_schedule(watch)
    await watch.run()
    assert len(texts(notifier, ADMIN)) == 2


async def test_an_undelivered_partial_notice_is_made_again(
    services: Any, notifier: Any, yk: FakeYooKassa, watch: RefundWatch
) -> None:
    await paid_order(services)
    yk.pay(refunded="100.00")
    notifier.telegram = AsyncMock(return_value=False)
    await watch.run()
    forget_schedule(watch)
    await watch.run()
    assert notifier.telegram.await_count == 2


@pytest.mark.parametrize(
    "changes",
    [
        {"id": "00000000-0000-4000-8000-000000000000"},
        {"status": "canceled"},
    ],
    ids=["other-id", "not-succeeded"],
)
async def test_an_answer_that_is_not_this_payment_is_ignored(
    services: Any, yk: FakeYooKassa, watch: RefundWatch, changes: dict[str, Any]
) -> None:
    await paid_order(services)
    yk.pay(refunded="299.00", **changes)
    assert await watch.run() == 0
    assert (await services.db.get_payment(ORDER))["status"] == "paid"


async def test_a_refund_in_another_currency_is_ignored(
    services: Any, yk: FakeYooKassa, watch: RefundWatch
) -> None:
    await paid_order(services)
    yk.pay(refunded="299.00")
    yk.payments[YID]["refunded_amount"]["currency"] = "USD"
    assert await watch.run() == 0
    assert (await services.db.get_payment(ORDER))["status"] == "paid"


# ------------------------------------------------------------------ failures
async def test_an_order_of_another_shop_is_tried_again_tomorrow_quietly(
    services: Any, notifier: Any, yk: FakeYooKassa, watch: RefundWatch
) -> None:
    await paid_order(services, "tgyk:")  # YooKassa answers 404: not in this shop
    assert await watch.run() == 0
    assert texts(notifier, ADMIN) == []
    assert watch._next[ORDER] >= time.time() + UNREACHABLE_RETRY - 5


async def test_a_provider_outage_is_survived_and_retried(
    services: Any, notifier: Any, yk: FakeYooKassa, watch: RefundWatch
) -> None:
    await paid_order(services)
    yk.fail = YooKassaError("get_payment: transport error ConnectTimeout")
    assert await watch.run() == 0
    assert (await services.db.get_payment(ORDER))["status"] == "paid"
    yk.fail = None
    yk.pay(refunded="299.00")
    forget_schedule(watch)
    assert await watch.run() == 1


async def test_numbers_that_are_not_yookassa_ids_are_not_asked_about(
    services: Any, yk: FakeYooKassa, watch: RefundWatch
) -> None:
    await services.db.create_user(USER, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(USER)
    await services.db.create_payment(
        payment_id="order-r2",
        telegram_id=USER,
        amount=PAID,
        currency="RUB",
        months=1,
        method="sbp",
        provider_id="tgyk:yk-refund-1",
    )
    await services.provisioning.process_paid_payment("order-r2")
    assert await watch.run() == 0
    assert yk.calls == []


async def test_without_api_keys_the_watch_does_nothing(
    services: Any, notifier: Any
) -> None:
    quiet = RefundWatch(services.db, None, services.provisioning, notifier, ADMIN)
    await paid_order(services)
    assert await quiet.run() == 0


async def test_one_run_asks_about_a_limited_number_of_orders(
    services: Any, yk: FakeYooKassa, watch: RefundWatch
) -> None:
    await services.db.create_user(USER, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(USER)
    for n in range(25):
        await services.db.create_payment(
            payment_id=f"bulk-{n}",
            telegram_id=USER,
            amount=PAID,
            currency="RUB",
            months=1,
            method="sbp",
            provider_id=f"yookassa:2f3a9c1e-000f-5000-8000-{n:012d}",
        )
        await services.provisioning.process_paid_payment(f"bulk-{n}")
    await watch.run()
    assert len(yk.calls) == 20
    await watch.run()  # the rest, without asking the first twenty again
    assert len(yk.calls) == 25


# --------------------------------------------------------- the owner's command
async def test_refund_command_also_takes_back_a_link_payment(
    services: Any,
) -> None:
    before = await paid_order(services)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=ADMIN), answer=AsyncMock(), chat=None
    )
    await cmd_refund(
        message,  # type: ignore[arg-type]
        CommandObject(command="refund", args=ORDER),
        services,
    )
    assert YID in message.answer.await_args.args[0]
    assert (await services.db.get_payment(ORDER))["status"] == "refunded"
    after = await services.db.get_user(USER)
    assert int(after["expires_at"]) < int(before["expires_at"])


# ------------------------------------------------------------------- wiring
async def test_the_job_is_scheduled_and_runs_the_watch(
    services: Any, notifier: Any, watch: RefundWatch
) -> None:
    jobs = Jobs(
        services.db,
        services.provisioning,
        notifier,
        services.monitor,
        services.settings,
        [],
        refunds=watch,
    )
    scheduler = build_scheduler(jobs)
    job = scheduler.get_job("refund_watch")
    assert job is not None
    assert job.trigger.interval.total_seconds() == 600
    watch.run = AsyncMock(return_value=0)  # type: ignore[method-assign]
    await jobs.refund_watch()
    watch.run.assert_awaited_once()


async def test_user_card_lists_the_recent_orders_for_a_refund(services: Any) -> None:
    await paid_order(services)
    message = SimpleNamespace(answer=AsyncMock(), from_user=SimpleNamespace(id=ADMIN))
    await cmd_user(
        message,  # type: ignore[arg-type]
        CommandObject(command="user", args=str(USER)),
        services,
    )
    card = message.answer.await_args.args[0]
    assert "Последние заказы (номер для /refund)" in card
    assert f"<code>{ORDER}</code> 299,00 ₽, 1 мес., paid" in card
