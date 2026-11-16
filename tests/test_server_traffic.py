"""Server traffic: the billing period, the learned forecast, owner warnings,
the trial pause near the hosting plan's allowance, and where the data comes from."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from config import GIB
from handlers.admin import cmd_traffic
from services.scheduler import Jobs, build_scheduler
from services.server_traffic import (
    DECIMAL_GB,
    ServerTrafficWatch,
    TrafficOutlook,
    alerts,
    billing_period,
    build_outlook,
    fit,
    report,
)

ROOT = Path(__file__).resolve().parents[1]
GB = DECIMAL_GB
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)  # half of 11 November is over


def _days(first: date, last: date, per_day: float) -> dict[date, int]:
    """Panel bytes per day: ``per_day`` GB for every day, half of it for NOW's day."""
    days: dict[date, int] = {}
    day = first
    while day <= last:
        share = 0.5 if day == NOW.date() else 1.0
        days[day] = int(per_day * GB * share)
        day += timedelta(days=1)
    return days


def _outlook(days: dict[date, int], **changes: Any) -> TrafficOutlook:
    options: dict[str, Any] = {
        "now": NOW,
        "limit_gb": 5000,
        "counts": "out",
        "reset_day": 1,
        "clients": 10,
    }
    options.update(changes)
    return build_outlook(days, **options)


# ------------------------------------------------------------------ period
@pytest.mark.parametrize(
    ("today", "reset_day", "start", "end"),
    [
        (date(2026, 11, 8), 1, date(2026, 11, 1), date(2026, 12, 1)),
        (date(2026, 11, 8), 15, date(2026, 10, 15), date(2026, 11, 15)),
        (date(2026, 11, 15), 15, date(2026, 11, 15), date(2026, 12, 15)),
        (date(2026, 12, 20), 15, date(2026, 12, 15), date(2027, 1, 15)),
        (date(2027, 1, 3), 15, date(2026, 12, 15), date(2027, 1, 15)),
        (date(2027, 3, 1), 28, date(2027, 2, 28), date(2027, 3, 28)),
    ],
)
def test_billing_period_follows_the_reset_day(
    today: date, reset_day: int, start: date, end: date
) -> None:
    assert billing_period(today, reset_day) == (start, end)


def test_billing_period_rejects_days_missing_in_short_months() -> None:
    with pytest.raises(ValueError, match="reset_day"):
        billing_period(date(2026, 11, 8), 31)


# ------------------------------------------------------------------- model
def test_short_history_uses_the_mean() -> None:
    model = fit([90.0, 100.0, 110.0])
    assert model is not None
    assert model.method == "mean"
    assert model.day(1) == pytest.approx(100.0)
    assert model.day(9) == pytest.approx(100.0)
    assert fit([]) is None


def test_steady_traffic_is_forecast_steady() -> None:
    model = fit([100.0] * 20)
    assert model is not None
    assert model.method == "holt"
    assert model.day(1) == pytest.approx(100.0)
    assert model.day(10) == pytest.approx(100.0)
    assert model.sigma == pytest.approx(0.0, abs=1e-9)


def test_growth_is_learned_and_damped() -> None:
    # A growing customer base: +10 GB every day.
    model = fit([100.0 + 10 * t for t in range(30)])
    assert model is not None
    assert model.day(1) == pytest.approx(400.0, rel=0.05)
    assert model.day(10) > model.day(1)
    # Damping: the trend slows down instead of growing without end.
    assert model.day(30) - model.day(20) < 10 * (model.day(11) - model.day(1))


def test_noise_widens_the_bound_but_keeps_the_mean() -> None:
    series = [100.0 + (15 if t % 2 else -15) for t in range(28)]
    model = fit(series)
    assert model is not None
    assert model.sigma > 0
    assert model.day(5) == pytest.approx(100.0, rel=0.2)


# ----------------------------------------------------------------- outlook
def test_steady_month_is_forecast_from_the_pace() -> None:
    outlook = _outlook(_days(date(2026, 10, 1), NOW.date(), 100))
    assert outlook.method == "holt"
    assert outlook.history_days == 41  # 1 October .. 10 November
    assert outlook.period_start == date(2026, 11, 1)
    assert outlook.period_end == date(2026, 12, 1)
    # 10.5 days at 105 GB (100 GB from the panel plus 5 % on the wire).
    assert outlook.used_bytes == pytest.approx(10.5 * 105 * GB, rel=0.01)
    assert outlook.expected_bytes == pytest.approx(30 * 105 * GB, rel=0.01)
    assert outlook.high_bytes is not None
    assert outlook.expected_bytes is not None
    assert outlook.high_bytes >= outlook.expected_bytes
    assert outlook.daily_bytes == pytest.approx(105 * GB, rel=0.01)
    assert outlook.exhaust_day is None
    assert not outlook.trials_paused
    assert alerts(outlook) == []


def test_both_directions_double_the_estimate() -> None:
    days = _days(date(2026, 10, 1), NOW.date(), 100)
    out = _outlook(days, counts="out")
    both = _outlook(days, counts="both")
    assert out.expected_bytes is not None
    assert both.expected_bytes is not None
    assert both.expected_bytes == pytest.approx(2 * out.expected_bytes, rel=0.01)


def test_overrun_names_the_day_the_allowance_ends() -> None:
    # 210 GB a day from the panel, both directions billed: 441 GB a day.
    outlook = _outlook(_days(date(2026, 10, 1), NOW.date(), 210), counts="both")
    assert outlook.expected_share is not None
    assert outlook.expected_share > 2
    assert outlook.used_share == pytest.approx(10.5 * 441 / 5000, rel=0.01)
    # 5000 GB / 441 GB a day ≈ 11.3 days after 1 November.
    assert outlook.exhaust_day == date(2026, 11, 12)
    levels = [level for level, _ in alerts(outlook)]
    assert levels == ["used_90", "forecast_100"]
    assert outlook.near_limit
    assert not outlook.trials_paused  # the pause is off unless switched on


def test_days_without_data_are_filled_by_the_model() -> None:
    # The bot started on 6 November: 1-5 November are unknown.
    outlook = _outlook(_days(date(2026, 11, 6), NOW.date(), 100))
    assert outlook.method == "mean"
    assert outlook.estimated_days == 5
    assert outlook.used_bytes == pytest.approx(10.5 * 105 * GB, rel=0.01)
    assert "оценён моделью" in report(outlook)


def test_first_hours_use_todays_pace_and_no_data_gives_no_forecast() -> None:
    today_only = _outlook({NOW.date(): 50 * GB})
    assert today_only.method == "today"
    assert today_only.daily_bytes == pytest.approx(105 * GB, rel=0.01)
    assert alerts(today_only) == []  # a forecast warning needs 3 full days

    nothing = _outlook({})
    assert nothing.method == "none"
    assert nothing.expected_bytes is None
    assert nothing.used_bytes == 0
    assert "мало данных" in report(nothing)


def test_capacity_in_average_clients() -> None:
    outlook = _outlook(_days(date(2026, 10, 1), NOW.date(), 100), clients=10)
    # 105 GB a day for 10 clients: 315 GB per client per 30 days.
    assert outlook.client_month_bytes == pytest.approx(315 * GB, rel=0.01)
    assert outlook.capacity_clients == 15
    assert "примерно на 15" in report(outlook)


@pytest.mark.parametrize(
    ("used_gb", "level"),
    [(3990, None), (4000, "used_80"), (4500, "used_90"), (5000, "used_100")],
)
def test_usage_levels(used_gb: int, level: str | None) -> None:
    # Everything was used on 1 November, nothing since: no forecast warning.
    days = {date(2026, 11, 1): int(used_gb * GB / 1.05) + 10**6}
    days.update({date(2026, 11, 1) + timedelta(days=i): 0 for i in range(1, 11)})
    outlook = _outlook(days)
    found = [name for name, _ in alerts(outlook) if name.startswith("used")]
    assert found == ([level] if level else [])
    assert outlook.near_limit is (used_gb >= 4500)


def test_report_states_the_estimate_and_how_to_check_it() -> None:
    text = report(_outlook(_days(date(2026, 10, 1), NOW.date(), 100)))
    assert "с 1 ноября по 30 ноября" in text
    assert "осталось дней: 20" in text
    assert "SERVER_TRAFFIC_COUNTS=out" in text
    assert "панелью хостинга" in text
    assert "Пробный доступ: открыт" in text


# -------------------------------------------------------------- database
async def test_server_days_accumulate_and_zero_marks_a_seen_day(services: Any) -> None:
    db = services.db
    await db.add_server_traffic("2026-11-10", 0)
    await db.add_server_traffic("2026-11-11", 5)
    await db.add_server_traffic("2026-11-11", 7)
    await db.add_server_traffic("2026-11-11", -3)  # never subtracts
    assert await db.server_traffic_since("2026-11-01") == [
        ("2026-11-10", 0),
        ("2026-11-11", 12),
    ]
    assert await db.purge_server_traffic("2026-11-11") == 1


async def test_backfill_seeds_only_missing_days_from_the_user_table(
    services: Any,
) -> None:
    db = services.db
    db.activity_logging_enabled = True  # ACTIVITY_LOGGING_ENABLED=true
    for node, day, base, last in (
        ("n1", "2026-11-09", 100, 160),
        ("n2", "2026-11-09", 0, 40),
        ("n1", "2026-11-10", 160, 200),
        ("n3", "2026-11-10", 50, 10),  # counter reset: never negative
    ):
        await db.upsert_traffic_day(
            node, day, baseline_bytes=base, last_bytes=last, last_online_at=None
        )
    await db.add_server_traffic("2026-11-10", 999)  # already counted: kept
    await db.backfill_server_traffic("2026-11-11")
    assert await db.server_traffic_since("2026-11-01") == [
        ("2026-11-09", 100),
        ("2026-11-10", 999),
    ]


async def test_alert_claims_are_once_per_period(services: Any) -> None:
    db = services.db
    assert await db.claim_server_traffic_alert("2026-11-01", "used_80")
    assert not await db.claim_server_traffic_alert("2026-11-01", "used_80")
    assert await db.claim_server_traffic_alert("2026-12-01", "used_80")
    await db.release_server_traffic_alert("2026-11-01", "used_80")
    assert await db.claim_server_traffic_alert("2026-11-01", "used_80")
    assert await db.purge_server_traffic_alerts("2026-12-01") == 1


# ------------------------------------------------------------------- watch
async def _fill(db: Any, per_day_gb: float, first: date, last: date) -> None:
    for day, value in _days(first, last, per_day_gb).items():
        await db.add_server_traffic(day.isoformat(), value)


async def test_watch_warns_once_and_pauses_new_trials(
    services: Any, notifier: Any, hiddify: Any
) -> None:
    settings = services.settings.model_copy(
        update={"server_traffic_counts": "both", "server_traffic_pause_trials": True}
    )
    watch = ServerTrafficWatch(services.db, notifier, settings)
    services.provisioning.trial_gate = watch.trials_paused
    await _fill(services.db, 210, date(2026, 10, 1), NOW.date())

    await watch.refresh(NOW)
    admin = [t for who, t in notifier.messages if who == settings.admin_telegram_id]
    assert len(admin) == 2
    assert "пробные доступы приостановлены" in admin[0]
    assert "Прогноз превышения" in admin[1]
    await watch.refresh(NOW + timedelta(hours=1))
    again = [t for who, t in notifier.messages if who == settings.admin_telegram_id]
    assert len(again) == 2  # once per period and level

    await services.db.create_user(830001, "new", None)
    result = await services.provisioning.accept_offer_and_start_trial(830001)
    assert (result.ok, result.reason) == (False, "trials_paused")
    user = await services.db.get_user(830001)
    assert user is not None
    assert await services.db.has_accepted_offer(830001)
    assert not user["trial_used"]
    assert not hiddify.users


async def test_without_the_switch_trials_go_on_and_the_owner_is_told(
    services: Any, notifier: Any
) -> None:
    settings = services.settings.model_copy(update={"server_traffic_counts": "both"})
    assert settings.server_traffic_pause_trials is False  # the default
    watch = ServerTrafficWatch(services.db, notifier, settings)
    services.provisioning.trial_gate = watch.trials_paused
    await _fill(services.db, 210, date(2026, 10, 1), NOW.date())
    outlook = await watch.refresh(NOW)
    assert outlook.near_limit
    admin = [t for who, t in notifier.messages if who == settings.admin_telegram_id]
    assert "автопауза выключена" in admin[0]
    assert "Пробный доступ: открыт (автопауза выключена)" in report(outlook)
    await services.db.create_user(830003, "new", None)
    result = await services.provisioning.accept_offer_and_start_trial(830003)
    assert result.ok


async def test_failed_delivery_is_retried(services: Any, notifier: Any) -> None:
    settings = services.settings.model_copy(update={"server_traffic_counts": "both"})
    watch = ServerTrafficWatch(services.db, notifier, settings)
    await _fill(services.db, 210, date(2026, 10, 1), NOW.date())
    notifier.telegram = AsyncMock(return_value=False)
    await watch.refresh(NOW)
    assert notifier.telegram.await_count == 2
    await watch.refresh(NOW)
    assert notifier.telegram.await_count == 4


async def test_trials_stay_open_without_an_outlook(
    services: Any, notifier: Any
) -> None:
    watch = ServerTrafficWatch(services.db, notifier, services.settings)
    assert not watch.trials_paused()
    services.provisioning.trial_gate = watch.trials_paused
    await services.db.create_user(830002, "new", None)
    result = await services.provisioning.accept_offer_and_start_trial(830002)
    assert result.ok


async def test_purge_keeps_about_a_year(services: Any, notifier: Any) -> None:
    watch = ServerTrafficWatch(services.db, notifier, services.settings)
    await services.db.add_server_traffic("2025-01-01", 1)
    await services.db.add_server_traffic("2026-11-01", 1)
    await watch.purge(NOW)
    assert await services.db.server_traffic_since("2000-01-01") == [("2026-11-01", 1)]


# ------------------------------------------------------------ data source
async def test_poll_adds_every_subscribers_traffic_to_the_day(
    services: Any, hiddify: Any
) -> None:
    nodes = []
    for telegram_id in (830010, 830011):
        await services.db.create_user(telegram_id, "user", None)
        await services.provisioning.accept_offer_and_start_trial(telegram_id)
        user = await services.db.get_user(telegram_id)
        nodes.append(user["node_username"])

    def panel(used: list[int]) -> None:
        hiddify.panel_usage = [
            {
                "username": node,
                "status": "active",
                "used_traffic": value,
                "lifetime_used_traffic": value,
                "data_limit": 0,
            }
            for node, value in zip(nodes, used, strict=True)
        ]

    panel([10 * GIB, 20 * GIB])
    await services.monitor.poll()  # the first poll sets each user's baseline
    panel([15 * GIB, 23 * GIB])
    await services.monitor.poll()
    rows = await services.db.server_traffic_since("2000-01-01")
    assert len(rows) == 1
    assert rows[0][1] == 8 * GIB


async def test_job_runs_hourly(services: Any, notifier: Any) -> None:
    watch = ServerTrafficWatch(services.db, notifier, services.settings)
    jobs = Jobs(
        services.db,
        services.provisioning,
        notifier,
        services.monitor,
        services.settings,
        [],
        traffic=watch,
    )
    scheduler = build_scheduler(jobs)
    assert scheduler.get_job("server_traffic") is not None
    await jobs.server_traffic()
    assert watch.outlook is not None


# ------------------------------------------------------------------ owner
async def test_owner_command(services: Any, notifier: Any) -> None:
    message = MagicMock()
    message.answer = AsyncMock()
    await cmd_traffic(message, services)
    assert "выключен" in message.answer.await_args.args[0]

    services.traffic = ServerTrafficWatch(services.db, notifier, services.settings)
    await services.db.add_server_traffic(datetime.now(UTC).date().isoformat(), 0)
    await cmd_traffic(message, services)
    assert "Трафик сервера" in message.answer.await_args.args[0]


def test_owner_can_set_the_allowance_with_the_update_script() -> None:
    script = (ROOT / "scripts" / "update_bot.sh").read_text(encoding="utf-8")
    for key in (
        "SERVER_TRAFFIC_LIMIT_GB",
        "SERVER_TRAFFIC_COUNTS",
        "SERVER_TRAFFIC_RESET_DAY",
        "SERVER_TRAFFIC_PAUSE_TRIALS",
    ):
        assert key in script.split("SETTABLE_KEYS=", 1)[1].split('"', 2)[1]
        assert f'"$key" == {key}' in script
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SERVER_TRAFFIC_LIMIT_GB=5000" in example
    assert "SERVER_TRAFFIC_PAUSE_TRIALS=false" in example


async def test_the_bot_wires_the_watch_only_when_a_limit_is_set(
    services: Any,
) -> None:
    import main

    built = main.build_services(services.settings, services.db, MagicMock())
    assert built.traffic is not None
    assert built.provisioning.trial_gate == built.traffic.trials_paused
    off = services.settings.model_copy(update={"server_traffic_limit_gb": 0})
    built = main.build_services(off, services.db, MagicMock())
    assert built.traffic is None
    assert built.provisioning.trial_gate is None
