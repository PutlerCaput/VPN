from __future__ import annotations

import time
from datetime import timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

from aiogram.exceptions import TelegramNetworkError
from aiogram.methods import SendMessage

import services.scheduler as scheduler_module
from services.notifier import Notifier
from services.scheduler import Jobs, build_scheduler
from tests.conftest import FakeNotifier

DAY_SECONDS = 24 * 60 * 60


async def _set_subscription(
    db: Any, telegram_id: int, *, status: str, expires_at: int
) -> None:
    await db.create_user(telegram_id, "user", None)
    await db.set_subscription(
        telegram_id,
        status=status,
        expires_at=expires_at,
        node_username=f"u{telegram_id}",
        config_link=None,
    )


async def test_expiry_reminder_is_sent_once_when_subscription_enters_three_day_window(
    db: Any,
    services: Any,
    notifier: FakeNotifier,
    monkeypatch: Any,
) -> None:
    current = int(time.time())
    monkeypatch.setattr(scheduler_module, "time", SimpleNamespace(time=lambda: current))

    await _set_subscription(
        db, 810001, status="active", expires_at=current + 3 * DAY_SECONDS
    )
    await _set_subscription(
        db, 810002, status="active", expires_at=current + 4 * DAY_SECONDS
    )
    await _set_subscription(
        db, 810003, status="suspended", expires_at=current + DAY_SECONDS
    )
    await _set_subscription(
        db, 810004, status="expired", expires_at=current + DAY_SECONDS
    )
    await _set_subscription(
        db, 810006, status="active", expires_at=current + DAY_SECONDS
    )
    await db._execute(
        "UPDATE users SET created_at = ? WHERE telegram_id = ?",
        (current - 60, 810001),
    )

    jobs = Jobs(
        db,
        services.provisioning,
        cast(Notifier, notifier),
        services.monitor,
        services.settings,
        [],
    )
    await jobs.expiry_reminder()
    await jobs.expiry_reminder()

    assert [telegram_id for telegram_id, _ in notifier.messages] == [810001]
    assert "Подписка заканчивается" in notifier.messages[0][1]
    user = await db.get_user(810001)
    assert user["expiry_reminded_for"] == current + 3 * DAY_SECONDS


async def test_renewed_subscription_can_receive_a_new_reminder(
    db: Any,
    services: Any,
    notifier: FakeNotifier,
    monkeypatch: Any,
) -> None:
    current = int(time.time())
    clock = [current]
    monkeypatch.setattr(
        scheduler_module, "time", SimpleNamespace(time=lambda: clock[0])
    )
    await _set_subscription(
        db, 810005, status="active", expires_at=current + 30 * DAY_SECONDS
    )
    jobs = Jobs(
        db,
        services.provisioning,
        cast(Notifier, notifier),
        services.monitor,
        services.settings,
        [],
    )

    await jobs.expiry_reminder()
    assert not notifier.messages

    clock[0] = current + 27 * DAY_SECONDS
    await jobs.expiry_reminder()
    assert len(notifier.messages) == 1

    new_expiry = current + 60 * DAY_SECONDS
    await db.set_subscription(
        810005,
        status="active",
        expires_at=new_expiry,
        node_username="u810005",
        config_link=None,
    )
    clock[0] = current + 57 * DAY_SECONDS
    await jobs.expiry_reminder()

    assert len(notifier.messages) == 2
    user = await db.get_user(810005)
    assert user["expiry_reminded_for"] == new_expiry


def test_expiry_reminder_scheduler_polls_every_fifteen_minutes(
    services: Any, notifier: FakeNotifier
) -> None:
    jobs = Jobs(
        services.db,
        services.provisioning,
        cast(Notifier, notifier),
        services.monitor,
        services.settings,
        [],
    )
    scheduler = build_scheduler(jobs)
    job = scheduler.get_job("expiry_reminder")
    assert job is not None
    assert job.trigger.interval == timedelta(minutes=15)


async def test_telegram_failure_does_not_mark_reminder_delivered(
    db: Any, services: Any, monkeypatch: Any
) -> None:
    current = int(time.time())
    expires = current + 3 * DAY_SECONDS
    monkeypatch.setattr(scheduler_module, "time", SimpleNamespace(time=lambda: current))
    await _set_subscription(db, 810007, status="active", expires_at=expires)
    await db._execute(
        "UPDATE users SET created_at = ? WHERE telegram_id = ?",
        (current - 60, 810007),
    )
    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramNetworkError(
            method=SendMessage(chat_id=810007, text="reminder"), message="timeout"
        ),
        None,
    ]
    push = AsyncMock()
    real_notifier = Notifier(bot, push)
    jobs = Jobs(
        db,
        services.provisioning,
        real_notifier,
        services.monitor,
        services.settings,
        [],
    )

    await jobs.expiry_reminder()
    assert (await db.get_user(810007))["expiry_reminded_for"] is None
    await jobs.expiry_reminder()
    assert (await db.get_user(810007))["expiry_reminded_for"] == expires
    await jobs.expiry_reminder()
    assert bot.send_message.await_count == 2


async def test_failed_push_does_not_repeat_successful_telegram_reminder(
    db: Any, services: Any, monkeypatch: Any
) -> None:
    current = int(time.time())
    expires = current + 3 * DAY_SECONDS
    monkeypatch.setattr(scheduler_module, "time", SimpleNamespace(time=lambda: current))
    await _set_subscription(db, 810008, status="active", expires_at=expires)
    await db._execute(
        "UPDATE users SET created_at = ? WHERE telegram_id = ?",
        (current - 60, 810008),
    )
    bot = AsyncMock()
    push = AsyncMock()
    push.send.side_effect = OSError("push unavailable")
    jobs = Jobs(
        db,
        services.provisioning,
        Notifier(bot, push),
        services.monitor,
        services.settings,
        [],
    )
    await jobs.expiry_reminder()
    await jobs.expiry_reminder()
    assert (await db.get_user(810008))["expiry_reminded_for"] == expires
    assert bot.send_message.await_count == 1
