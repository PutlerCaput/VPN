"""/stats: counts and sums for the owner, and nothing that names a customer."""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from handlers.admin import cmd_stats
from services.owner_stats import format_stats, money_line, percent, rubles

DAY = 86400
ALICE, BOB, CAROL, DAVE = 990001, 990002, 990003, 990004


async def customer(services: Any, telegram_id: int, name: str) -> None:
    await services.db.create_user(telegram_id, name, None)
    await services.provisioning.accept_offer_and_start_trial(telegram_id)


async def pay(services: Any, telegram_id: int, payment_id: str, amount: int) -> None:
    await services.db.create_payment(
        payment_id=payment_id,
        telegram_id=telegram_id,
        amount=amount,
        currency="RUB",
        months=1,
        method="sbp",
    )
    await services.provisioning.process_paid_payment(payment_id)


def test_money_is_written_the_russian_way() -> None:
    assert rubles(29900) == "299,00 ₽"
    assert rubles(119600) == "1 196,00 ₽"
    assert rubles(5) == "0,05 ₽"
    assert percent(3, 5) == "60%"
    assert percent(0, 0) == "—"
    rows = [
        {"currency": "RUB", "period": "now", "n": 2, "total": 59800},
        {"currency": "XTR", "period": "now", "n": 1, "total": 150},
        {"currency": "RUB", "period": "before", "n": 1, "total": 29900},
    ]
    assert money_line(rows, "now") == "59 800,00 ₽"[:0] + "598,00 ₽ (2), 150 ⭐ (1)"
    assert money_line(rows, "before") == "299,00 ₽ (1)"
    assert money_line([], "now") == "—"


async def test_the_counts_follow_the_customers(services: Any) -> None:
    for telegram_id, name in ((ALICE, "alice"), (BOB, "bob"), (CAROL, "carol")):
        await customer(services, telegram_id, name)
    await pay(services, ALICE, "a" * 32, 29900)
    await pay(services, BOB, "b" * 32, 29900)
    await services.db.mark_payment_refunded("b" * 32)  # Bob got the money back
    now = int(time.time())
    # Carol's paid time ends in two days; Dave's ended yesterday.
    await services.db.set_subscription(
        CAROL,
        status="active",
        expires_at=now + 2 * DAY,
        node_username=(await services.db.get_user(CAROL))["node_username"],
        config_link=None,
    )
    await customer(services, DAVE, "dave")
    await services.db.set_user_status(DAVE, "expired")
    await services.db._execute(
        "UPDATE users SET expires_at = ? WHERE telegram_id = ?", (now - DAY, DAVE)
    )

    stats = await services.db.owner_stats(now)
    users = stats["users"]
    assert users["total"] == 4
    assert users["expired"] == 1
    assert users["trials"] == 4
    assert users["new_7d"] == 4
    assert users["expiring_3d"] >= 1  # Carol (the others' dates are later)
    assert users["lapsed_7d"] == 1  # Dave
    assert stats["paying_users"] == 1  # Alice; Bob's payment was refunded
    assert stats["refunds_30d"] == 1
    rub = [m for m in stats["money"] if m["currency"] == "RUB" and m["period"] == "now"]
    assert [(m["n"], m["total"]) for m in rub] == [(1, 29900)]


async def test_the_text_has_the_numbers_and_no_customer_in_it(services: Any) -> None:
    await customer(services, ALICE, "alice_private_name")
    await pay(services, ALICE, "a" * 32, 29900)
    now = int(time.time())
    text = format_stats(await services.db.owner_stats(now), now)
    assert "Клиенты: всего 1" in text
    assert "Пробный → платный: 1 из 1 (100%)" in text
    assert "299,00 ₽ (1)" in text
    assert "Возвраты за 30 дней: 0" in text
    for private in (str(ALICE), "alice_private_name", "a" * 32, "u" + str(ALICE)):
        assert private not in text


async def test_an_empty_service_gives_a_readable_page(services: Any) -> None:
    now = int(time.time())
    text = format_stats(await services.db.owner_stats(now), now)
    assert "Клиенты: всего 0" in text
    assert "Пробный → платный: 0 из 0 (—)" in text
    assert "Оплаты за 30 дней: —" in text


async def test_the_command_answers_the_owner_and_adds_the_traffic(
    services: Any, notifier: Any
) -> None:
    from services.server_traffic import ServerTrafficWatch

    watch = ServerTrafficWatch(services.db, notifier, services.settings)
    await watch.compute()
    services.traffic = watch
    message = SimpleNamespace(answer=AsyncMock(), from_user=SimpleNamespace(id=1))
    await cmd_stats(message, services)  # type: ignore[arg-type]
    text = message.answer.await_args.args[0]
    assert text.startswith("📊 <b>Сводка</b>")
    assert "Трафик сервера:" in text
