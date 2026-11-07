"""Traffic quota: trial and tier limits, notices, profile line, link reset."""

from __future__ import annotations

from typing import Any

import pytest
from aiohttp.test_utils import TestClient, TestServer

from config import GIB, Settings
from handlers.common import status_text, traffic_line
from services.provisioning import ProvisioningError, ProvisioningService, TrafficStatus
from tests.conftest import make_init_data
from webhook_server import create_webhook_app


def _with(services: Any, **changes: Any) -> None:
    services.settings = services.settings.model_copy(update=changes)
    services.provisioning.settings = services.settings


async def _trial(services: Any, telegram_id: int) -> dict[str, Any]:
    await services.db.create_user(telegram_id, "user", None)
    result = await services.provisioning.accept_offer_and_start_trial(telegram_id)
    assert result.ok
    user = await services.db.get_user(telegram_id)
    assert user is not None
    return user


async def test_trial_gets_the_configured_quota_and_first_tier_on_sale(
    services: Any, marzban: Any
) -> None:
    _with(services, trial_traffic_gb=3, enabled_tiers_csv="standard")
    user = await _trial(services, 610001)
    assert user["tier"] == "standard"
    panel = marzban.users[user["node_username"]]
    assert panel["data_limit"] == 3 * GIB
    assert panel["data_limit_reset_strategy"] == "no_reset"
    assert panel["note"] == "tier=standard devices=2"


async def test_default_trial_stays_unlimited(services: Any, marzban: Any) -> None:
    user = await _trial(services, 610002)
    assert user["tier"] == "standard"
    assert marzban.users[user["node_username"]]["data_limit"] == 0


async def test_paid_period_sets_the_tier_quota_and_bonus_keeps_it(
    services: Any, marzban: Any
) -> None:
    _with(services, trial_traffic_gb=3)
    user = await _trial(services, 610003)
    payment = await services.billing.create(610003, 2, "sbp", "standard")
    assert await services.provisioning.process_paid_payment(payment.payment_id)
    panel = marzban.users[user["node_username"]]
    assert (panel["data_limit"], panel["data_limit_reset_strategy"]) == (
        200 * GIB,
        "month",
    )
    await services.provisioning.add_bonus_days(610003, 3, "test")
    assert panel["data_limit"] == 200 * GIB
    # Buying an original unlimited tier lifts the limit again.
    legacy = await services.billing.create(610003, 1, "sbp", "basic")
    assert await services.provisioning.process_paid_payment(legacy.payment_id)
    assert panel["data_limit"] == 0


def _panel(
    node: str, used: float, limit: float, *, lifetime: float | None = None, **extra: Any
) -> dict[str, Any]:
    return {
        "username": node,
        "status": extra.pop("status", "active"),
        "used_traffic": int(used * GIB),
        "lifetime_used_traffic": int(
            (lifetime if lifetime is not None else used) * GIB
        ),
        "data_limit": int(limit * GIB),
        "data_limit_reset_strategy": extra.pop("strategy", "month"),
        **extra,
    }


async def test_quota_notices_once_per_level_and_window(
    services: Any, notifier: Any
) -> None:
    user = await _trial(services, 610004)
    node = user["node_username"]
    monitor = services.monitor

    def notices() -> list[str]:
        return [text for target, text in notifier.messages if target == 610004]

    start = len(notices())
    await monitor.check_user(_panel(node, 100, 200))
    assert len(notices()) == start  # below 80%
    await monitor.check_user(_panel(node, 161, 200))
    await monitor.check_user(_panel(node, 170, 200))
    warnings = notices()[start:]
    assert len(warnings) == 1
    assert "80%" in warnings[0]
    assert "161 из 200 ГБ" in warnings[0]

    await monitor.check_user(_panel(node, 200, 200, status="limited"))
    await monitor.check_user(_panel(node, 200, 200, status="limited"))
    assert len(notices()[start:]) == 2
    assert "израсходован" in notices()[-1]

    # The panel reset the counter: a new 30-day window warns again.
    await monitor.check_user(_panel(node, 165, 200, lifetime=365))
    assert len(notices()[start:]) == 3


async def test_trial_quota_notice_points_to_payment(
    services: Any, notifier: Any
) -> None:
    user = await _trial(services, 610005)
    await services.monitor.check_user(
        _panel(user["node_username"], 3, 3, status="limited", strategy="no_reset")
    )
    assert "Оплата" in notifier.messages[-1][1]


async def test_undelivered_notice_is_retried(services: Any, notifier: Any) -> None:
    user = await _trial(services, 610006)
    node = user["node_username"]
    original = notifier.notify

    async def failing(*args: Any, **kwargs: Any) -> bool:
        return False

    notifier.notify = failing
    await services.monitor.check_user(_panel(node, 170, 200))
    notifier.notify = original
    before = len(notifier.messages)
    await services.monitor.check_user(_panel(node, 171, 200))
    assert len(notifier.messages) == before + 1


async def test_unlimited_and_inactive_users_get_no_quota_notice(
    services: Any, notifier: Any
) -> None:
    user = await _trial(services, 610007)
    node = user["node_username"]
    before = len(notifier.messages)
    await services.monitor.check_user(_panel(node, 5000, 0))
    await services.db.set_user_status(610007, "expired")
    await services.monitor.check_user(_panel(node, 199, 200))
    assert len(notifier.messages) == before


def test_traffic_line_and_status() -> None:
    limited = TrafficStatus(int(200 * GIB), int(200 * GIB), "month", True)
    assert "200 из 200 ГБ" in traffic_line(limited)
    assert "каждые 30 дней" in traffic_line(limited)
    assert "израсходован" in traffic_line(limited)
    unlimited = TrafficStatus(int(1.5 * GIB), 0, "no_reset", False)
    assert traffic_line(unlimited) == "<b>Трафик:</b> 1.5 ГБ, без ограничения"
    user = {
        "status": "active",
        "expires_at": 2_000_000_000,
        "tier": "standard",
        "node_username": "u610008",
    }
    text = status_text(user, limited)
    assert "Стандарт (до 2 устройств, 200 ГБ на 30 дней)" in text
    assert "Трафик" in text


async def test_traffic_status_reads_the_panel(services: Any, marzban: Any) -> None:
    user = await _trial(services, 610009)
    marzban.users[user["node_username"]]["used_traffic"] = 5 * GIB
    status = await services.provisioning.traffic_status(user)
    assert status == TrafficStatus(5 * GIB, 0, "no_reset", False)
    assert await services.provisioning.traffic_status({"node_username": None}) is None


async def test_revoke_link_issues_new_credentials(services: Any, marzban: Any) -> None:
    user = await _trial(services, 610010)
    old = user["config_link"]
    new = await services.provisioning.revoke_link(610010)
    assert new != old
    assert (await services.db.get_user(610010))["config_link"] == new
    assert ("revoke", user["node_username"]) in marzban.calls
    await services.db.set_user_status(610010, "expired")
    with pytest.raises(ProvisioningError):
        await services.provisioning.revoke_link(610010)


async def test_api_reports_traffic_and_new_periods(services: Any, marzban: Any) -> None:
    services.settings = Settings(
        autoban_mode="automatic",
        enabled_tiers="standard,basic",  # type: ignore[call-arg]
        stars_enabled=True,
    )
    services.provisioning.settings = services.settings
    app = create_webhook_app(services.bot, None, services)  # type: ignore[arg-type]
    token = services.settings.bot_token.get_secret_value()
    async with TestClient(TestServer(app)) as client:
        auth = await client.post(
            "/api/v1/auth/telegram", json={"initData": make_init_data(token, 610011)}
        )
        headers = {"Authorization": f"Bearer {(await auth.json())['access_token']}"}
        await client.post("/api/v1/legal/accept", headers=headers)
        user = await services.db.get_user(610011)
        marzban.users[user["node_username"]]["used_traffic"] = 2 * GIB

        sub = await (await client.get("/api/v1/subscription", headers=headers)).json()
        assert sub["traffic"]["used_bytes"] == 2 * GIB
        assert sub["traffic"]["exhausted"] is False

        tariffs = await (await client.get("/api/v1/tariffs", headers=headers)).json()
        standard = tariffs["tiers"][0]
        assert standard["key"] == "standard"
        assert standard["traffic_gb"] == 200
        assert [p["months"] for p in standard["prices"]] == [1, 2, 3, 4, 5, 6]
        assert standard["prices"][0]["stars"] == 187

        created = await client.post(
            "/api/v1/payments/create",
            json={"months": 5, "method": "sbp", "tier": "standard"},
            headers=headers,
        )
        assert created.status == 201
        bad = await client.post(
            "/api/v1/payments/create",
            json={"months": 5, "method": "sbp", "tier": "basic"},
            headers=headers,
        )
        assert bad.status == 400


def test_provisioning_type_is_exported() -> None:
    assert ProvisioningService.revoke_link


async def test_trial_profile_shows_trial_not_paid_quota(services: Any) -> None:
    from handlers.common import is_trial

    _with(services, trial_traffic_gb=3, enabled_tiers_csv="standard")
    user = await _trial(services, 610012)
    assert await is_trial(services.db, user)
    text = status_text(user, trial=True)
    assert "пробный период (Стандарт, до 2 устройств)" in text
    assert "200 ГБ" not in text
    payment = await services.billing.create(610012, 1, "sbp", "standard")
    await services.provisioning.process_paid_payment(payment.payment_id)
    paid = await services.db.get_user(610012)
    assert not await is_trial(services.db, paid)


async def test_traffic_status_is_cached_and_refreshed_after_payment(
    services: Any, marzban: Any
) -> None:
    user = await _trial(services, 610013)
    node = user["node_username"]
    marzban.users[node]["used_traffic"] = 1 * GIB
    first = await services.provisioning.traffic_status(user)
    marzban.users[node]["used_traffic"] = 2 * GIB
    assert await services.provisioning.traffic_status(user) == first
    payment = await services.billing.create(610013, 1, "sbp", "standard")
    await services.provisioning.process_paid_payment(payment.payment_id)
    fresh = await services.provisioning.traffic_status(user)
    assert fresh is not None
    assert fresh.used_bytes == 2 * GIB
    assert fresh.limit_bytes == 200 * GIB
