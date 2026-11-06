"""Tariff tiers: prices, savings, persistence, Marzban payload and the API."""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from config import (
    TIER_PRICES_RUB,
    TIER_PRICES_USDT,
    TIERS,
    Settings,
    device_note,
    savings_percent,
)
from db.database import Database
from services.billing import BillingError
from services.marzban import MarzbanClient
from services.yookassa import receipt_item_description
from tests.conftest import make_init_data
from webhook_server import create_webhook_app


def test_price_matrix_matches_the_tariff_sheet() -> None:
    assert TIER_PRICES_RUB == {
        "basic": {1: 300, 3: 800, 6: 1500, 12: 2700},
        "complete": {1: 450, 3: 1200, 6: 2200, 12: 4000},
        "prime": {1: 700, 3: 1800, 6: 3500, 12: 6000},
    }
    assert {key: tier.devices for key, tier in TIERS.items()} == {
        "basic": 5,
        "complete": 10,
        "prime": 15,
    }


def test_usdt_prices_cover_every_period_and_grow_with_the_tier() -> None:
    for months in (1, 3, 6, 12):
        values = [
            TIER_PRICES_USDT[key][months] for key in ("basic", "complete", "prime")
        ]
        assert values == sorted(values)
        assert all(v % Decimal("0.5") == 0 for v in values)


def test_savings_badge_is_computed_against_monthly_billing() -> None:
    assert savings_percent("basic", 1) == 0
    assert savings_percent("basic", 3) == 11  # 800 instead of 900
    assert savings_percent("basic", 12) == 25  # 2700 instead of 3600
    assert savings_percent("prime", 12) == 29  # 6000 instead of 8400
    for key in TIERS:
        badges = [savings_percent(key, m) for m in (1, 3, 6, 12)]
        assert badges == sorted(badges)


def test_only_delivered_features_are_advertised() -> None:
    text = " ".join(f for tier in TIERS.values() for f in tier.features).lower()
    for unbuilt in ("multi-hop", "выделенн", "облачн", "1 тб", "1tb"):
        assert unbuilt not in text


def test_enabled_tiers_setting_is_validated(settings: Settings) -> None:
    assert settings.enabled_tiers == ["basic", "complete", "prime"]
    with pytest.raises(ValueError, match="enabled_tiers"):
        Settings(**{**settings.model_dump(), "enabled_tiers": "gold"})  # type: ignore[arg-type]


async def test_marzban_payload_keeps_vision_flow_and_notes_the_device_limit() -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/admin/token":
            return httpx.Response(200, json={"access_token": "t"})
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"username": "u1", "links": []})

    client = MarzbanClient(
        "http://marzban.test",
        "admin",
        "pw-from-test",
        "VLESS TCP REALITY",
        transport=httpx.MockTransport(handler),
    )
    await client.create_user("u123456", expire=2_000_000_000, tier="prime")
    await client.close()
    body = seen[0]
    assert body["proxies"] == {"vless": {"flow": "xtls-rprx-vision"}}
    assert body["note"] == "tier=prime devices=15"
    # Marzban has no such field; sending it would be silently ignored.
    assert "ip_limit" not in body
    offline = MarzbanClient("http://x", "a", "b", "t")
    with pytest.raises(ValueError, match="unknown tier"):
        await offline.create_user("u123456", tier="gold")
    await offline.close()


async def _paying_user(services: Any, telegram_id: int) -> None:
    await services.db.create_user(telegram_id, "buyer", None)
    await services.provisioning.accept_offer_and_start_trial(telegram_id)


async def test_payment_stores_tier_and_price(services: Any) -> None:
    await _paying_user(services, 770001)
    payment = await services.billing.create(770001, 6, "sbp", "complete")
    stored = await services.db.get_payment(payment.payment_id)
    assert stored["tier"] == "complete"
    assert stored["amount"] == 2200 * 100
    usdt_payment = await services.billing.create(770001, 3, "sbp", "prime")
    assert (await services.db.get_payment(usdt_payment.payment_id))[
        "amount"
    ] == 1800 * 100


async def test_unknown_or_disabled_tier_is_rejected(services: Any) -> None:
    await _paying_user(services, 770002)
    with pytest.raises(BillingError) as unknown:
        await services.billing.create(770002, 1, "sbp", "gold")
    assert unknown.value.code == "invalid_tier"
    services.billing.enabled_tiers = ["basic"]
    with pytest.raises(BillingError) as disabled:
        await services.billing.create(770002, 1, "sbp", "prime")
    assert disabled.value.code == "invalid_tier"
    await services.billing.create(770002, 1, "sbp", "basic")


async def test_paid_tier_applies_to_the_user_and_the_panel(services: Any) -> None:
    await _paying_user(services, 770003)
    assert (await services.db.get_user(770003))["tier"] == "basic"
    payment = await services.billing.create(770003, 1, "sbp", "complete")
    assert await services.provisioning.process_paid_payment(payment.payment_id)
    user = await services.db.get_user(770003)
    assert user["tier"] == "complete"
    assert services.marzban.users[user["node_username"]]["note"] == device_note(
        "complete"
    )


async def test_bonus_days_keep_the_current_tier(services: Any) -> None:
    await _paying_user(services, 770004)
    payment = await services.billing.create(770004, 1, "sbp", "prime")
    await services.provisioning.process_paid_payment(payment.payment_id)
    await services.provisioning.add_bonus_days(770004, 3, "test")
    assert (await services.db.get_user(770004))["tier"] == "prime"


def test_receipt_description_names_the_tier() -> None:
    assert receipt_item_description(3, "Complete") == (
        "Аренда удалённого технического узла, тариф Complete, 3 мес."
    )
    assert receipt_item_description(3, None) == (
        "Аренда удалённого технического узла, 3 мес."
    )


async def test_old_database_gets_tier_columns(tmp_path: Path) -> None:
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(
        """
        CREATE TABLE users (telegram_id INTEGER PRIMARY KEY, username TEXT,
            first_name TEXT, status TEXT NOT NULL DEFAULT 'pending', expires_at INTEGER,
            node_username TEXT UNIQUE, referred_by INTEGER, config_link TEXT,
            trial_used INTEGER NOT NULL DEFAULT 0, expiry_reminded_for INTEGER,
            created_at INTEGER NOT NULL);
        CREATE TABLE payments (payment_id TEXT PRIMARY KEY,
            telegram_id INTEGER NOT NULL, amount INTEGER NOT NULL,
            currency TEXT NOT NULL, months INTEGER NOT NULL,
            method TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            provider_id TEXT, paid_at INTEGER, created_at INTEGER NOT NULL);
        INSERT INTO users (telegram_id, status, created_at) VALUES (1, 'active', 1);
        INSERT INTO payments (payment_id, telegram_id, amount, currency, months,
            method, created_at) VALUES ('p1', 1, 30000, 'RUB', 1, 'sbp', 1);
        """
    )
    legacy.commit()
    legacy.close()
    db = Database(str(path))
    await db.connect()
    try:
        assert (await db.get_user(1))["tier"] == "basic"
        assert (await db.get_payment("p1"))["tier"] == "basic"
    finally:
        await db.close()


async def test_api_lists_enabled_tiers_and_creates_tiered_payments(
    services: Any,
) -> None:
    bot_token = services.settings.bot_token.get_secret_value()
    app = create_webhook_app(services.bot, None, services)  # type: ignore[arg-type]
    async with TestClient(TestServer(app)) as client:
        auth = await client.post(
            "/api/v1/auth/telegram",
            json={"initData": make_init_data(bot_token, 770005)},
        )
        headers = {"Authorization": f"Bearer {(await auth.json())['access_token']}"}
        tiers = (await (await client.get("/api/v1/tariffs", headers=headers)).json())[
            "tiers"
        ]
        assert [t["key"] for t in tiers] == ["basic", "complete", "prime"]
        prime = tiers[2]
        assert prime["devices"] == 15
        assert prime["prices"][-1] == {
            "months": 12,
            "rub": 6000,
            "usdt": "64.50",
            "save_percent": 29,
        }

        await client.post("/api/v1/legal/accept", headers=headers)
        created = await client.post(
            "/api/v1/payments/create",
            json={"months": 3, "method": "sbp", "tier": "complete"},
            headers=headers,
        )
        assert created.status == 201
        assert (await created.json())["amount_rub"] == 1200
        bad = await client.post(
            "/api/v1/payments/create",
            json={"months": 3, "method": "sbp", "tier": "gold"},
            headers=headers,
        )
        assert bad.status == 400

        services.billing.enabled_tiers = ["basic"]
        services.settings = services.settings.model_copy(
            update={"enabled_tiers_csv": "basic"}
        )
        only_basic = (
            await (await client.get("/api/v1/tariffs", headers=headers)).json()
        )["tiers"]
        assert [t["key"] for t in only_basic] == ["basic"]
        disabled = await client.post(
            "/api/v1/payments/create",
            json={"months": 1, "method": "sbp", "tier": "prime"},
            headers=headers,
        )
        assert disabled.status == 400


def test_bot_keyboards_offer_every_enabled_tier_and_fit_callback_limits() -> None:
    from handlers.payments import tiers_text
    from keyboards.main_menu import channel_kb, tariffs_kb, tiers_kb

    tier_rows = tiers_kb("sbp", ["basic", "prime"]).inline_keyboard
    assert [row[0].callback_data for row in tier_rows[:-1]] == [
        "tier:sbp:basic",
        "tier:sbp:prime",
    ]
    periods = tariffs_kb("usdt", "complete").inline_keyboard
    assert periods[1][0].callback_data == "pay:usdt:complete:3"
    assert "выгода 11%" in str(periods[1][0].text)
    deep = channel_kb(12, "prime").inline_keyboard
    assert deep[0][0].callback_data == "chan:sbp:prime:12"
    for rows in (tier_rows, periods, deep):
        for row in rows:
            for button in row:
                assert len((button.callback_data or "").encode()) <= 64
    overview = tiers_text("рубли", ["basic", "prime"])
    assert "Basic" in overview
    assert "Prime" in overview
    assert "Complete" not in overview
